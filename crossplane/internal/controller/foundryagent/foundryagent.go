// Package foundryagent reconciles FoundryAgent managed resources against the
// Foundry Agents data-plane API.
package foundryagent

import (
	"context"
	"errors"
	"fmt"
	"os"

	"github.com/Azure/azure-sdk-for-go/sdk/azidentity"
	xpv1 "github.com/crossplane/crossplane-runtime/apis/common/v1"
	"github.com/crossplane/crossplane-runtime/pkg/event"
	"github.com/crossplane/crossplane-runtime/pkg/logging"
	"github.com/crossplane/crossplane-runtime/pkg/reconciler/managed"
	"github.com/crossplane/crossplane-runtime/pkg/resource"
	corev1 "k8s.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	"github.com/rashadism/provider-foundry/apis/v1alpha1"
	"github.com/rashadism/provider-foundry/internal/clients"
)

// Setup wires the FoundryAgent controller into the manager.
func Setup(mgr ctrl.Manager, l logging.Logger) error {
	name := managed.ControllerName(v1alpha1.FoundryAgentGroupKind)
	r := managed.NewReconciler(mgr,
		resource.ManagedKind(v1alpha1.FoundryAgentGroupVersionKind),
		managed.WithExternalConnecter(&connector{kube: mgr.GetClient()}),
		managed.WithLogger(l.WithValues("controller", name)),
		managed.WithRecorder(event.NewAPIRecorder(mgr.GetEventRecorderFor(name))),
	)
	return ctrl.NewControllerManagedBy(mgr).
		Named(name).
		For(&v1alpha1.FoundryAgent{}).
		Complete(r)
}

type connector struct{ kube client.Client }

// Connect builds a Foundry client authenticated with DefaultAzureCredential
// (locally: your `az login`; in-cluster: workload identity).
func (c *connector) Connect(ctx context.Context, mg resource.Managed) (managed.ExternalClient, error) {
	cr, ok := mg.(*v1alpha1.FoundryAgent)
	if !ok {
		return nil, errors.New("managed resource is not a FoundryAgent")
	}
	cred, err := azidentity.NewDefaultAzureCredential(nil)
	if err != nil {
		return nil, err
	}
	// The endpoint is a per-environment infra constant, not a developer input.
	// Prefer it from the CR (back-compat); otherwise read the PE-provisioned
	// `foundry-account` ConfigMap so the ResourceType needn't prompt for it.
	endpoint := cr.Spec.ForProvider.ProjectEndpoint
	if endpoint == "" {
		if endpoint, err = c.endpointFromConfig(ctx); err != nil {
			return nil, err
		}
	}
	return &external{foundry: clients.New(cred, endpoint)}, nil
}

// endpointFromConfig reads the Foundry project endpoint from a PE-provisioned
// ConfigMap. Name/namespace default to `foundry-account` / the provider's own
// namespace and are overridable via FOUNDRY_CONFIG_NAME / FOUNDRY_CONFIG_NAMESPACE.
func (c *connector) endpointFromConfig(ctx context.Context) (string, error) {
	ns := os.Getenv("FOUNDRY_CONFIG_NAMESPACE")
	if ns == "" {
		ns = "provider-foundry"
	}
	name := os.Getenv("FOUNDRY_CONFIG_NAME")
	if name == "" {
		name = "foundry-account"
	}
	var cm corev1.ConfigMap
	if err := c.kube.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, &cm); err != nil {
		return "", fmt.Errorf("resolve project endpoint from configmap %s/%s: %w", ns, name, err)
	}
	ep := cm.Data["projectEndpoint"]
	if ep == "" {
		return "", fmt.Errorf("configmap %s/%s has no non-empty projectEndpoint key", ns, name)
	}
	return ep, nil
}

type external struct{ foundry *clients.Client }

func (e *external) Observe(ctx context.Context, mg resource.Managed) (managed.ExternalObservation, error) {
	cr := mg.(*v1alpha1.FoundryAgent)
	exists, def, err := e.foundry.Get(ctx, cr.Spec.ForProvider.AgentName)
	if err != nil {
		return managed.ExternalObservation{}, err
	}
	if !exists {
		return managed.ExternalObservation{ResourceExists: false}, nil
	}
	cr.Status.AtProvider.Exists = true
	cr.SetConditions(xpv1.Available())
	upToDate := def.Model == cr.Spec.ForProvider.Model &&
		def.Instructions == cr.Spec.ForProvider.Instructions
	return managed.ExternalObservation{ResourceExists: true, ResourceUpToDate: upToDate}, nil
}

func (e *external) Create(ctx context.Context, mg resource.Managed) (managed.ExternalCreation, error) {
	cr := mg.(*v1alpha1.FoundryAgent)
	cr.SetConditions(xpv1.Creating())
	p := cr.Spec.ForProvider
	return managed.ExternalCreation{}, e.foundry.Upsert(ctx, p.AgentName, p.Model, p.Instructions, mcpTools(p))
}

func (e *external) Update(ctx context.Context, mg resource.Managed) (managed.ExternalUpdate, error) {
	cr := mg.(*v1alpha1.FoundryAgent)
	p := cr.Spec.ForProvider
	return managed.ExternalUpdate{}, e.foundry.Upsert(ctx, p.AgentName, p.Model, p.Instructions, mcpTools(p))
}

func (e *external) Delete(ctx context.Context, mg resource.Managed) (managed.ExternalDelete, error) {
	cr := mg.(*v1alpha1.FoundryAgent)
	cr.SetConditions(xpv1.Deleting())
	return managed.ExternalDelete{}, e.foundry.Delete(ctx, cr.Spec.ForProvider.AgentName)
}

func (e *external) Disconnect(ctx context.Context) error { return nil }

// mcpTools builds the Foundry `tools` array from the CR's MCPTools.
func mcpTools(p v1alpha1.FoundryAgentParameters) []map[string]any {
	var tools []map[string]any
	for _, m := range p.MCPTools {
		ra := m.RequireApproval
		if ra == "" {
			ra = "never"
		}
		tools = append(tools, map[string]any{
			"type":             "mcp",
			"server_label":     m.ServerLabel,
			"server_url":       m.ServerURL,
			"require_approval": ra,
		})
	}
	return tools
}
