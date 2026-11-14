// Read-only Kubernetes v0.36.2 TypeChecker against captured API schemas.
// Run from the independent review Go module; never connects to Kubernetes.
package main

import (
	"encoding/json"
	"fmt"
	v1 "k8s.io/api/admissionregistration/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apiserver/pkg/admission/plugin/policy/validating"
	"k8s.io/apiserver/pkg/cel/openapi/resolver"
	"k8s.io/kube-openapi/pkg/validation/spec"
	"os"
	"strings"
)

type schemas map[string]*spec.Schema

func (s schemas) ResolveSchema(g schema.GroupVersionKind) (*spec.Schema, error) {
	var name string
	switch g.Kind {
	case "Deployment":
		name = "io.k8s.api.apps.v1.Deployment"
	case "Pod":
		name = "io.k8s.api.core.v1.Pod"
	case "ConfigMap":
		name = "io.k8s.api.core.v1.ConfigMap"
	default:
		return nil, resolver.ErrSchemaNotFound
	}
	return resolver.PopulateRefs(func(ref string) (*spec.Schema, bool) {
		v, ok := s[strings.TrimPrefix(ref, "#/components/schemas/")]
		return v, ok
	}, name)
}
func mustRead(path string) []byte {
	b, e := os.ReadFile(path)
	if e != nil {
		panic(e)
	}
	return b
}
func main() {
	all := schemas{}
	for _, path := range os.Args[2:] {
		var d struct {
			Components struct {
				Schemas schemas `json:"schemas"`
			} `json:"components"`
		}
		if e := json.Unmarshal(mustRead(path), &d); e != nil {
			panic(e)
		}
		for k, v := range d.Components.Schemas {
			all[k] = v
		}
	}
	mapper := meta.NewDefaultRESTMapper([]schema.GroupVersion{{Group: "apps", Version: "v1"}, {Group: "", Version: "v1"}})
	for _, g := range []schema.GroupVersionKind{{Group: "apps", Version: "v1", Kind: "Deployment"}, {Version: "v1", Kind: "Pod"}, {Version: "v1", Kind: "ConfigMap"}} {
		mapper.Add(g, meta.RESTScopeNamespace)
		if _, e := all.ResolveSchema(g); e != nil {
			panic(e)
		}
	}
	checker := validating.TypeChecker{SchemaResolver: all, RestMapper: mapper}
	var list struct {
		Items []json.RawMessage `json:"items"`
	}
	if e := json.Unmarshal(mustRead(os.Args[1]), &list); e != nil {
		panic(e)
	}
	count := 0
	warnings := 0
	for _, item := range list.Items {
		var p v1.ValidatingAdmissionPolicy
		if e := json.Unmarshal(item, &p); e != nil {
			panic(e)
		}
		if p.Kind != "ValidatingAdmissionPolicy" {
			continue
		}
		count++
		w := checker.Check(&p)
		for i, m := range p.Spec.MatchConditions {
			results := checker.CheckExpression(checker.CreateContext(&p), m.Expression)
			if len(results) > 0 {
				w = append(w, v1.ExpressionWarning{FieldRef: fmt.Sprintf("spec.matchConditions[%d].expression", i), Warning: results.String()})
			}
		}
		warnings += len(w)
		b, _ := json.Marshal(map[string]interface{}{"policy": p.Name, "typeChecking": map[string]interface{}{"expressionWarnings": w}})
		fmt.Println(string(b))
	}
	fmt.Printf("{\"policies\":%d,\"warnings\":%d}\n", count, warnings)
	if count == 0 || warnings != 0 {
		os.Exit(1)
	}
}
