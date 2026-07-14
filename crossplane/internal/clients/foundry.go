// Package clients is a thin data-plane client for the Foundry Agents REST API.
package clients

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/azcore/policy"
)

const scope = "https://ai.azure.com/.default"

// Client talks to one Foundry project's data-plane endpoint.
type Client struct {
	cred     azcore.TokenCredential
	http     *http.Client
	endpoint string // https://<acct>.services.ai.azure.com/api/projects/<project>
}

// New returns a Client for the given project endpoint.
func New(cred azcore.TokenCredential, endpoint string) *Client {
	return &Client{cred: cred, http: http.DefaultClient, endpoint: strings.TrimRight(endpoint, "/")}
}

func (c *Client) token(ctx context.Context) (string, error) {
	t, err := c.cred.GetToken(ctx, policy.TokenRequestOptions{Scopes: []string{scope}})
	if err != nil {
		return "", err
	}
	return t.Token, nil
}

func (c *Client) do(ctx context.Context, method, url string, body []byte) (int, []byte, error) {
	tok, err := c.token(ctx)
	if err != nil {
		return 0, nil, err
	}
	var r io.Reader
	if body != nil {
		r = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(ctx, method, url, r)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Authorization", "Bearer "+tok)
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, out, nil
}

// AgentDef is the observed prompt-agent definition.
type AgentDef struct {
	Model        string
	Instructions string
}

// Get returns whether the agent exists and its current definition.
func (c *Client) Get(ctx context.Context, name string) (bool, AgentDef, error) {
	url := fmt.Sprintf("%s/agents/%s?api-version=v1", c.endpoint, name)
	code, body, err := c.do(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false, AgentDef{}, err
	}
	if code == http.StatusNotFound {
		return false, AgentDef{}, nil
	}
	if code >= 300 {
		return false, AgentDef{}, fmt.Errorf("get agent: HTTP %d: %s", code, body)
	}
	// Definition lives under versions.latest.definition (or definition).
	var raw struct {
		Definition *struct {
			Model        string `json:"model"`
			Instructions string `json:"instructions"`
		} `json:"definition"`
		Versions *struct {
			Latest struct {
				Definition struct {
					Model        string `json:"model"`
					Instructions string `json:"instructions"`
				} `json:"definition"`
			} `json:"latest"`
		} `json:"versions"`
	}
	if err := json.Unmarshal(body, &raw); err != nil {
		return true, AgentDef{}, nil
	}
	d := AgentDef{}
	if raw.Definition != nil {
		d.Model, d.Instructions = raw.Definition.Model, raw.Definition.Instructions
	} else if raw.Versions != nil {
		d.Model = raw.Versions.Latest.Definition.Model
		d.Instructions = raw.Versions.Latest.Definition.Instructions
	}
	return true, d, nil
}

// Upsert creates or updates the prompt agent (POST is content-addressable).
func (c *Client) Upsert(ctx context.Context, name, model, instructions string) error {
	payload, _ := json.Marshal(map[string]any{
		"name": name,
		"definition": map[string]any{
			"kind":         "prompt",
			"model":        model,
			"instructions": instructions,
		},
	})
	url := fmt.Sprintf("%s/agents?api-version=v1", c.endpoint)
	code, body, err := c.do(ctx, http.MethodPost, url, payload)
	if err != nil {
		return err
	}
	if code >= 300 && code != http.StatusConflict {
		return fmt.Errorf("upsert agent: HTTP %d: %s", code, body)
	}
	return nil
}

// Delete removes the agent and all its versions.
func (c *Client) Delete(ctx context.Context, name string) error {
	url := fmt.Sprintf("%s/agents/%s?api-version=v1&force=true", c.endpoint, name)
	code, body, err := c.do(ctx, http.MethodDelete, url, nil)
	if err != nil {
		return err
	}
	if code >= 300 && code != http.StatusNotFound {
		return fmt.Errorf("delete agent: HTTP %d: %s", code, body)
	}
	return nil
}
