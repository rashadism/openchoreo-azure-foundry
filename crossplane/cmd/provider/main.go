// Command provider runs the Foundry Crossplane provider.
package main

import (
	"github.com/crossplane/crossplane-runtime/pkg/logging"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/log/zap"

	"github.com/rashadism/provider-foundry/apis/v1alpha1"
	"github.com/rashadism/provider-foundry/internal/controller/foundryagent"
)

func main() {
	zl := zap.New(zap.UseDevMode(true))
	ctrl.SetLogger(zl)
	log := logging.NewLogrLogger(zl)

	cfg, err := ctrl.GetConfig()
	if err != nil {
		panic(err)
	}
	mgr, err := ctrl.NewManager(cfg, ctrl.Options{})
	if err != nil {
		panic(err)
	}
	if err := v1alpha1.AddToScheme(mgr.GetScheme()); err != nil {
		panic(err)
	}
	if err := foundryagent.Setup(mgr, log); err != nil {
		panic(err)
	}
	log.Info("starting provider-foundry")
	if err := mgr.Start(ctrl.SetupSignalHandler()); err != nil {
		panic(err)
	}
}
