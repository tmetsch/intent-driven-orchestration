#!/usr/bin/env python3
"""
Analytics script to determine the scaling properties for a given workload.

WARNING: These scripts are for proof of concept only; For production system
please consider using more advanced analytical capabilities.
"""

import argparse
import base64
import dataclasses
import datetime
import io
import logging
import os

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pymongo

from matplotlib import cm
from matplotlib import ticker
from pymongo import errors
from scipy import optimize
from scipy import stats

FIG_SIZE = (8, 6)
FORMAT = '%(asctime)s - %(filename)-15s - %(threadName)-10s - ' \
         '%(funcName)-24s - %(lineno)-3s - %(levelname)-7s - %(message)s'
logging.basicConfig(format=FORMAT, level=logging.INFO)


def latency_func(data, p_0, p_1, p_2):
    """
    Latency function relating throughput, replicas and latency.
    """
    tput = data[0, :]
    n_pods = data[1, :]
    # 1st part: higher traffic --> higher latency.
    # 2nd part: more replicas --> lower latency.
    return p_0 + p_1 * tput * np.log(tput) - p_2 * np.log(n_pods)


def get_data(args):
    """
    Return data from MongoDB. This is for demo purposes only, in production
    systems this should go back to the storage parts of the observability
    stack.
    """
    client = pymongo.MongoClient(args.mongo_uri)
    dbs = client["intents"]
    coll = dbs["events"]

    tmp = {}
    res = coll.find({"name": args.name},
                    {"current_objectives": 1,
                     "pods": 1,
                     "timestamp": 1,
                     "_id": 0}, sort=[('_id', -1)], limit=args.max_vals)
    for item in res:
        if item["pods"] is None:
            continue
        tmp[item["timestamp"]] = item["current_objectives"]
        tmp[item["timestamp"]]["replicas"] = len(
            ["0" for pod in item["pods"].values()
                if pod['state'] == 'Running']
        )
    data = pd.DataFrame.from_dict(tmp, orient="index")
    cols = data.columns
    data.drop_columns = data.drop
    data.drop_columns(columns=[item for item in cols if
                               item not in (args.throughput,
                                            args.latency,
                                            'replicas')],
                      inplace=True)
    return data


def store_result(popt,
                 data,
                 img,
                 args):
    """
    Store the results back to knowledge base.
    """
    client = pymongo.MongoClient(args.mongo_uri)
    dbs = client["intents"]
    coll = dbs["effects"]

    throughput_range = (min(data[args.throughput]), max(data[args.throughput]))
    replica_range = (min(data["replicas"]), max(data["replicas"]))
    training_features = [args.throughput, "replicas"]
    timestamp = datetime.datetime.now()
    doc = {"name": args.name,
           "profileName": args.latency,
           "group": "scaling",
           "data": {"throughputRange": throughput_range,
                    "replicaRange": replica_range,
                    "popt": popt.tolist(),
                    "trainingFeatures": training_features,
                    "targetFeature": args.latency,
                    "image": img},
           "static": False,
           "timestamp": timestamp}
    try:
        coll.insert_one(doc)
    except errors.ExecutionTimeout as err:
        logging.error("Connection to database timed out - could not store "
                      "effect: %s", err)


def analyse(data, args):
    """
    If enough unique data-points are available - do a curve fit and return
    parameters.
    """
    if data.empty or \
            len(data.index) < args.min_vals or \
            len(data["replicas"].unique()) < 2:
        logging.warning("Not enough (unique) data points available.")
        logging.debug("Data was: %s", data)

    # some cleanup
    data = data[(data[args.latency] != -1.0) & (data[args.throughput] != -1.0)]
    data = data.dropna()

    # remove outliers
    df_0 = data[(np.abs(stats.zscore(data)) < 3).all(axis=1)]
    if df_0.empty:
        logging.warning("After removing outliers the dataframe was empty...")
        logging.debug("Data was: %s", data)
        return None, None

    # do the actual curve fitting
    tmp = [df_0[args.throughput], df_0["replicas"]]
    try:
        popt, _ = optimize.curve_fit(latency_func, tmp, df_0[args.latency],
                                     full_output=False)
    except ValueError as err:
        logging.warning("Could not curve fit: %s.", err)
        return None, None

    # check if we have a proper model
    if popt[2] > 0.0 and popt.sum() != 1.0:
        return popt, data
    logging.warning("Found inverted plane for: %s:%s (%s) - will discard.",
                    args.name, args.latency, popt)
    return None, None


def plot_results(data, popt, args):
    """
    Visualize the results and return base6 encoded img.
    """
    fig = plt.Figure(figsize=FIG_SIZE)
    axes = fig.add_subplot(projection="3d")

    tput_min, tput_max = data[args.throughput].min(), data[
        args.throughput].max()
    rep_min, rep_max = data["replicas"].min(), data["replicas"].max()

    tput = np.linspace(tput_min, tput_max, num=10)
    replicas = np.linspace(rep_min, rep_max + 1, num=10)
    x_1, y_1 = np.meshgrid(tput, replicas)
    lat = latency_func(np.array([x_1, y_1]), *popt)

    axes.scatter(data[args.throughput], data["replicas"], data[args.latency],
                 marker="o", color="black", alpha=0.5)
    axes.plot_surface(x_1, y_1, lat,
                      cmap=cm.get_cmap("viridis"), linewidth=0.25,
                      antialiased=True, edgecolor="black", alpha=0.75)
    axes.view_init(20, -45)

    axes.set_title(f"Scaling effect on {args.latency} for {args.name}.")
    axes.set_xlabel("throughput")
    axes.set_ylabel("# of PODs")
    axes.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    axes.set_zlabel("latency / ($ms$)")

    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png")
    tmp = buf.getbuffer()

    fig.clf()
    return base64.b64encode(tmp).decode("ascii")


def main(args):
    """
    Main logic.
    """
    data = get_data(args)
    popt, data = analyse(data, args)
    if popt is not None:
        img = plot_results(data, popt, args)
        store_result(popt,
                     data,
                     img,
                     args)
        logging.info("Found plane for: %s:%s (%s).",
                     args.name, args.latency, popt)


@dataclasses.dataclass
class Arguments:
    """
    Dataclass that hold the arguments as parsed from argparse.
    """

    name: str
    latency: str
    throughput: str
    min_vals: int
    max_vals: int
    mongo_uri: str

    @staticmethod
    def from_args(args):
        """
        Set the arguments based on the parsed data.
        """
        return Arguments(
            name=args['name'],
            latency=args['latency'],
            throughput=args['throughput'],
            min_vals=int(args['min_vals']),
            max_vals=int(args['max_vals']),
            mongo_uri=args['mongo_uri'],
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("name", type=str,
                        help="Name of the objective.")
    parser.add_argument("latency", type=str,
                        help="Name of the latency objective.")
    parser.add_argument("throughput", type=str,
                        help="Name of the throughput objective.")
    parser.add_argument("--min_vals", type=int, default=10,
                        help="Amount of features we want to collect before "
                             "even training a model")
    parser.add_argument("--max_vals", type=int, default=500,
                        help="Amount of features we want to include in the "
                             "model (basically defines how long we look back "
                             "in time).")
    parser.add_argument("--mongo_uri", type=str,
                        default=os.environ.get('MONGO_URL',
                                               'mongodb://localhost:27100'),
                        help="Mongo connection string.")
    parsed_args = dict(**vars(parser.parse_args()))
    arguments = Arguments.from_args(parsed_args)
    main(arguments)
