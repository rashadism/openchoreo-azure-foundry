package v1alpha1

import (
	xpv1 "github.com/crossplane/crossplane-runtime/apis/common/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// FoundryAgentParameters is the desired state of an agent.
type FoundryAgentParameters struct {
	// ProjectEndpoint of an existing Foundry project.
	ProjectEndpoint string `json:"projectEndpoint"`

	AgentName string `json:"agentName"`
	Image     string `json:"image"`

	ModelDeploymentName string `json:"modelDeploymentName,omitempty"`

	// +kubebuilder:default="1"
	CPU string `json:"cpu,omitempty"`
	// +kubebuilder:default="2Gi"
	Memory string `json:"memory,omitempty"`
	// +kubebuilder:default="responses"
	Protocol string `json:"protocol,omitempty"`
}

// FoundryAgentObservation is the last-seen state from Azure.
type FoundryAgentObservation struct {
	Version string `json:"version,omitempty"`
	Status  string `json:"status,omitempty"` // creating | active | failed
}

type FoundryAgentSpec struct {
	xpv1.ResourceSpec `json:",inline"`
	ForProvider       FoundryAgentParameters `json:"forProvider"`
}

type FoundryAgentStatus struct {
	xpv1.ResourceStatus `json:",inline"`
	AtProvider          FoundryAgentObservation `json:"atProvider,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="READY",type="string",JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="STATUS",type="string",JSONPath=".status.atProvider.status"
type FoundryAgent struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   FoundryAgentSpec   `json:"spec"`
	Status FoundryAgentStatus `json:"status,omitempty"`
}
