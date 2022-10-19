package main

import (
	"flag"

	"github.com/intel/intent-driven-orchestration/pkg/controller"

	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/tools/clientcmd"

	"os"
	"os/signal"

	plugins "github.com/intel/intent-driven-orchestration/pkg/api/plugins/v1alpha1"
	"github.com/intel/intent-driven-orchestration/pkg/common"
	"github.com/intel/intent-driven-orchestration/pkg/planner"
	"github.com/intel/intent-driven-orchestration/pkg/planner/actuators"
	"github.com/intel/intent-driven-orchestration/pkg/planner/actuators/platform"

	"k8s.io/klog/v2"
)

var (
	kubeConfig string
	config     string
)

// RdtPluginHandler represents the actual actuator.
type RdtPluginHandler struct {
	actuator actuators.Actuator
}

func (s *RdtPluginHandler) NextState(state *common.State, goal *common.State, profiles map[string]common.Profile) ([]common.State, []float64, []planner.Action) {
	klog.InfoS("Invoked rdt Next State Callback")
	return s.actuator.NextState(state, goal, profiles)
}

func (s *RdtPluginHandler) Perform(state *common.State, plan []planner.Action) {
	klog.InfoS("Invoked rdt Perform Callback")
	s.actuator.Perform(state, plan)
}

func (s *RdtPluginHandler) Effect(state *common.State, profiles map[string]common.Profile) {
	klog.InfoS("Invoked rdt Effect Callback")
	s.actuator.Effect(state, profiles)
}

func main() {
	klog.InitFlags(nil)
	flag.Parse()
	tmp, err := common.LoadConfig(config, func() interface{} {
		return &platform.RdtConfig{}
	})
	cfg := tmp.(*platform.RdtConfig)
	if err != nil {
		klog.Fatalf("Error loading configuration for actuator: %s", err)
	}
	mt := controller.NewMongoTracer(cfg.MongoEndpoint)
	config, err := clientcmd.BuildConfigFromFlags("", kubeConfig)
	if err != nil {
		klog.Fatalf("Error getting incluster k8s config: %s", err)
	}
	clusterClient, err := kubernetes.NewForConfig(config)
	if err != nil {
		klog.Fatalf("Error creating k8s cluster client: %s", err)
	}

	p := &RdtPluginHandler{
		actuator: platform.NewRdtActuator(clusterClient, mt, *cfg),
	}
	stub := plugins.NewActuatorPluginStub(p.actuator.Name(), cfg.Endpoint, cfg.Port, cfg.PluginManagerEndpoint, cfg.PluginManagerPort)
	stub.SetNextStateFunc(p.NextState)
	stub.SetPerformFunc(p.Perform)
	stub.SetEffectFunc(p.Effect)
	err = stub.Start()
	if err != nil {
		klog.Fatalf("Error starting plugin server: %s", err)
	}
	err = stub.Register()
	if err != nil {
		klog.Fatalf("Error registering plugin: %s", err)
	}
	signalChan := make(chan os.Signal, 1)
	signal.Notify(signalChan, os.Interrupt)
	<-signalChan
	err = stub.Stop()
	if err != nil {
		klog.Fatalf("Error stopping plugin server: %s", err)
	}
}

func init() {
	flag.StringVar(&kubeConfig, "kubeConfig", "", "Path to a kube config file.")
	flag.StringVar(&config, "config", "", "Path to configuration file.")
}
