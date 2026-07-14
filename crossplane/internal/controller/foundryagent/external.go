// Package foundryagent implements the Crossplane ExternalClient for a FoundryAgent.
// It is a skeleton: the REST client (foundryAPI) is declared, not implemented.
//
// Method signatures track your crossplane-runtime version; adjust if they differ.
package foundryagent

import (
	"context"

	xpv1 "github.com/crossplane/crossplane-runtime/apis/common/v1"
	"github.com/crossplane/crossplane-runtime/pkg/reconciler/managed"
	"github.com/crossplane/crossplane-runtime/pkg/resource"

	"github.com/rashadism/provider-foundry/apis/v1alpha1"
)

// foundryAPI is the thin data-plane client. Auth: workload identity token for
// https://ai.azure.com/. To be implemented.
type foundryAPI interface {
	GetAgent(ctx context.Context, endpoint, name string) (agent, error)
	CreateAgent(ctx context.Context, p v1alpha1.FoundryAgentParameters) error
	CreateVersion(ctx context.Context, p v1alpha1.FoundryAgentParameters) error
	DeleteAgent(ctx context.Context, endpoint, name string) error
}

type agent struct {
	Status string
}

// matches reports whether the live agent equals the desired spec (image, cpu, ...).
func (a agent) matches(p v1alpha1.FoundryAgentParameters) bool { return false }

func isNotFound(err error) bool { return false }

// external satisfies managed.ExternalClient.
type external struct {
	foundry foundryAPI
}

func (c *external) Observe(ctx context.Context, mg resource.Managed) (managed.ExternalObservation, error) {
	cr := mg.(*v1alpha1.FoundryAgent)

	live, err := c.foundry.GetAgent(ctx, cr.Spec.ForProvider.ProjectEndpoint, cr.Spec.ForProvider.AgentName)
	if isNotFound(err) {
		return managed.ExternalObservation{ResourceExists: false}, nil
	}
	if err != nil {
		return managed.ExternalObservation{}, err
	}

	cr.Status.AtProvider.Status = live.Status
	if live.Status == "active" {
		cr.SetConditions(xpv1.Available())
	}

	return managed.ExternalObservation{
		ResourceExists:   true,
		ResourceUpToDate: live.matches(cr.Spec.ForProvider),
	}, nil
}

func (c *external) Create(ctx context.Context, mg resource.Managed) (managed.ExternalCreation, error) {
	cr := mg.(*v1alpha1.FoundryAgent)
	cr.SetConditions(xpv1.Creating())
	return managed.ExternalCreation{}, c.foundry.CreateAgent(ctx, cr.Spec.ForProvider)
}

func (c *external) Update(ctx context.Context, mg resource.Managed) (managed.ExternalUpdate, error) {
	cr := mg.(*v1alpha1.FoundryAgent)
	return managed.ExternalUpdate{}, c.foundry.CreateVersion(ctx, cr.Spec.ForProvider)
}

func (c *external) Delete(ctx context.Context, mg resource.Managed) (managed.ExternalDeletion, error) {
	cr := mg.(*v1alpha1.FoundryAgent)
	cr.SetConditions(xpv1.Deleting())
	return managed.ExternalDeletion{}, c.foundry.DeleteAgent(ctx, cr.Spec.ForProvider.ProjectEndpoint, cr.Spec.ForProvider.AgentName)
}
