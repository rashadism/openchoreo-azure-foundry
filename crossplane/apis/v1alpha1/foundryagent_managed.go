package v1alpha1

import xpv1 "github.com/crossplane/crossplane-runtime/apis/common/v1"

// These methods make *FoundryAgent satisfy crossplane-runtime's resource.Managed
// interface. (angryjet generates these in a real provider; hand-written here.)

func (mg *FoundryAgent) GetCondition(ct xpv1.ConditionType) xpv1.Condition {
	return mg.Status.GetCondition(ct)
}
func (mg *FoundryAgent) GetDeletionPolicy() xpv1.DeletionPolicy { return mg.Spec.DeletionPolicy }
func (mg *FoundryAgent) GetManagementPolicies() xpv1.ManagementPolicies {
	return mg.Spec.ManagementPolicies
}
func (mg *FoundryAgent) GetProviderConfigReference() *xpv1.Reference {
	return mg.Spec.ProviderConfigReference
}
func (mg *FoundryAgent) GetPublishConnectionDetailsTo() *xpv1.PublishConnectionDetailsTo {
	return mg.Spec.PublishConnectionDetailsTo
}
func (mg *FoundryAgent) GetWriteConnectionSecretToReference() *xpv1.SecretReference {
	return mg.Spec.WriteConnectionSecretToReference
}

func (mg *FoundryAgent) SetConditions(c ...xpv1.Condition) { mg.Status.SetConditions(c...) }
func (mg *FoundryAgent) SetDeletionPolicy(p xpv1.DeletionPolicy) {
	mg.Spec.DeletionPolicy = p
}
func (mg *FoundryAgent) SetManagementPolicies(p xpv1.ManagementPolicies) {
	mg.Spec.ManagementPolicies = p
}
func (mg *FoundryAgent) SetProviderConfigReference(r *xpv1.Reference) {
	mg.Spec.ProviderConfigReference = r
}
func (mg *FoundryAgent) SetPublishConnectionDetailsTo(p *xpv1.PublishConnectionDetailsTo) {
	mg.Spec.PublishConnectionDetailsTo = p
}
func (mg *FoundryAgent) SetWriteConnectionSecretToReference(r *xpv1.SecretReference) {
	mg.Spec.WriteConnectionSecretToReference = r
}
