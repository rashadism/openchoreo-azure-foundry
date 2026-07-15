package v1alpha1

import (
	xpv1 "github.com/crossplane/crossplane-runtime/apis/common/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
)

// MCPTool attaches an MCP tool server to the agent.
type MCPTool struct {
	ServerLabel string `json:"serverLabel"`
	ServerURL   string `json:"serverURL"`
	// RequireApproval: "never" (default) auto-approves tool calls.
	RequireApproval string `json:"requireApproval,omitempty"`
}

// FoundryAgentParameters is the desired state of a prompt agent.
type FoundryAgentParameters struct {
	// ProjectEndpoint of an existing Foundry project, e.g.
	// https://<account>.services.ai.azure.com/api/projects/<project>
	ProjectEndpoint string `json:"projectEndpoint"`
	// AgentName is the agent's unique name within the project.
	AgentName string `json:"agentName"`
	// Model deployment name the agent uses.
	Model string `json:"model"`
	// Instructions define the agent's behaviour.
	Instructions string `json:"instructions"`
	// MCPTools attaches MCP tool servers to the agent.
	MCPTools []MCPTool `json:"mcpTools,omitempty"`
}

// FoundryAgentObservation is the observed state from Azure.
type FoundryAgentObservation struct {
	Exists  bool   `json:"exists,omitempty"`
	Version string `json:"version,omitempty"`
}

// A FoundryAgentSpec defines the desired state of a FoundryAgent.
type FoundryAgentSpec struct {
	xpv1.ResourceSpec `json:",inline"`
	ForProvider       FoundryAgentParameters `json:"forProvider"`
}

// A FoundryAgentStatus represents the observed state of a FoundryAgent.
type FoundryAgentStatus struct {
	xpv1.ResourceStatus `json:",inline"`
	AtProvider          FoundryAgentObservation `json:"atProvider,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="READY",type="string",JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="SYNCED",type="string",JSONPath=".status.conditions[?(@.type=='Synced')].status"
// +kubebuilder:printcolumn:name="AGENT",type="string",JSONPath=".spec.forProvider.agentName"
// +kubebuilder:resource:scope=Cluster,categories={crossplane,managed,foundry}

// A FoundryAgent is a prompt agent in an Azure AI Foundry project.
type FoundryAgent struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   FoundryAgentSpec   `json:"spec"`
	Status FoundryAgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// FoundryAgentList contains a list of FoundryAgent.
type FoundryAgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []FoundryAgent `json:"items"`
}

// FoundryAgent type metadata.
var (
	FoundryAgentKind             = "FoundryAgent"
	FoundryAgentGroupKind        = schema.GroupKind{Group: Group, Kind: FoundryAgentKind}.String()
	FoundryAgentGroupVersionKind = SchemeGroupVersion.WithKind(FoundryAgentKind)
)

func init() {
	SchemeBuilder.Register(&FoundryAgent{}, &FoundryAgentList{})
}
