// Package akilent is the official Go SDK for the Akilent email API.
//
//	client := akilent.New("ak_live_…")
//	msg, err := client.Messages.Send(ctx, akilent.SendParams{
//		From: "billing@acme.com", To: "user@example.com",
//		Subject: "Receipt", Text: "Thanks!",
//	})
package akilent

import (
	"net/http"
	"strings"
	"time"
)

// DefaultBaseURL is the API root used when Config.BaseURL is empty.
const DefaultBaseURL = "https://akilent.com"

const userAgent = "akilent-go/0.1.0"

// Config configures a Client. Only APIKey is required.
type Config struct {
	APIKey     string
	BaseURL    string
	HTTPClient *http.Client
	MaxRetries int
}

// Client is the entry point to the API. Create one with New and share it.
type Client struct {
	cfg Config

	Messages    *MessagesService
	Templates   *TemplatesService
	Campaigns   *CampaignsService
	Contacts    *ContactsService
	Events      *EventsService
	Workflows   *WorkflowsService
	RequestLogs *RequestLogsService
}

// New builds a Client from an API key using default settings.
func New(apiKey string) *Client {
	return NewWithConfig(Config{APIKey: apiKey})
}

// NewWithConfig builds a Client from a full Config.
func NewWithConfig(cfg Config) *Client {
	if cfg.BaseURL == "" {
		cfg.BaseURL = DefaultBaseURL
	}
	cfg.BaseURL = strings.TrimRight(cfg.BaseURL, "/")
	if cfg.HTTPClient == nil {
		cfg.HTTPClient = &http.Client{Timeout: 30 * time.Second}
	}
	if cfg.MaxRetries == 0 {
		cfg.MaxRetries = 2
	}
	c := &Client{cfg: cfg}
	c.Messages = &MessagesService{c}
	c.Templates = &TemplatesService{c}
	c.Campaigns = &CampaignsService{c}
	c.Contacts = &ContactsService{c}
	c.Events = &EventsService{c}
	c.Workflows = &WorkflowsService{c}
	c.RequestLogs = &RequestLogsService{c}
	return c
}
