package akilent

import (
	"context"
	"fmt"
	"net/url"
	"strconv"
)

// ---- Messages ---------------------------------------------------------------

type MessagesService struct{ c *Client }

// SendParams is the body for Messages.Send.
type SendParams struct {
	From      string         `json:"from"`
	To        string         `json:"to"`
	Subject   string         `json:"subject,omitempty"`
	Text      string         `json:"text,omitempty"`
	HTML      string         `json:"html,omitempty"`
	Template  string         `json:"template,omitempty"`
	Variables map[string]any `json:"template_variables,omitempty"`
	Locale    string         `json:"locale,omitempty"`
	// IdempotencyKey overrides the auto-generated key when non-empty.
	IdempotencyKey string `json:"-"`
}

// Message is the accepted-send response.
type Message struct {
	ID       int    `json:"id"`
	PublicID string `json:"public_id"`
	Status   string `json:"status"`
}

func (s *MessagesService) Send(ctx context.Context, p SendParams) (*Message, error) {
	var out Message
	err := s.c.do(ctx, "POST", "/api/v1/messages", nil, p, p.IdempotencyKey, &out)
	if err != nil {
		return nil, err
	}
	return &out, nil
}

// List returns one page of messages. Pass filters like {"status": "delivered"}.
func (s *MessagesService) List(ctx context.Context, filters map[string]string) (map[string]any, error) {
	q := url.Values{}
	for k, v := range filters {
		q.Set(k, v)
	}
	var out map[string]any
	err := s.c.do(ctx, "GET", "/api/v1/messages", q, nil, "", &out)
	return out, err
}

func (s *MessagesService) Get(ctx context.Context, id string) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "GET", "/api/v1/messages/"+id, nil, nil, "", &out)
	return out, err
}

func (s *MessagesService) Events(ctx context.Context, id string) ([]map[string]any, error) {
	var out struct {
		Data []map[string]any `json:"data"`
	}
	err := s.c.do(ctx, "GET", "/api/v1/messages/"+id+"/events", nil, nil, "", &out)
	return out.Data, err
}

// ---- Templates ------------------------------------------------------------

type TemplatesService struct{ c *Client }

func (s *TemplatesService) List(ctx context.Context) (any, error) {
	var out any
	err := s.c.do(ctx, "GET", "/api/v1/templates", nil, nil, "", &out)
	return out, err
}

func (s *TemplatesService) Create(ctx context.Context, body map[string]any) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "POST", "/api/v1/templates", nil, body, "", &out)
	return out, err
}

func (s *TemplatesService) Get(ctx context.Context, slug string) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "GET", "/api/v1/templates/"+slug, nil, nil, "", &out)
	return out, err
}

func (s *TemplatesService) Update(ctx context.Context, slug string, body map[string]any) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "PATCH", "/api/v1/templates/"+slug, nil, body, "", &out)
	return out, err
}

func (s *TemplatesService) Delete(ctx context.Context, slug string) error {
	return s.c.do(ctx, "DELETE", "/api/v1/templates/"+slug, nil, nil, "", nil)
}

func (s *TemplatesService) Preview(ctx context.Context, slug string, variables map[string]any, locale string) (map[string]any, error) {
	body := map[string]any{"variables": variables}
	if locale != "" {
		body["locale"] = locale
	}
	var out map[string]any
	err := s.c.do(ctx, "POST", "/api/v1/templates/"+slug+"/preview", nil, body, "", &out)
	return out, err
}

func (s *TemplatesService) UpsertLocale(ctx context.Context, slug, locale string, body map[string]any) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "PUT", "/api/v1/templates/"+slug+"/locales/"+locale, nil, body, "", &out)
	return out, err
}

// ---- Campaigns / Contacts / Events / Workflows / RequestLogs -------------

type CampaignsService struct{ c *Client }

func (s *CampaignsService) Create(ctx context.Context, body map[string]any) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "POST", "/api/v1/campaigns", nil, body, "", &out)
	return out, err
}

func (s *CampaignsService) Get(ctx context.Context, id int) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "GET", "/api/v1/campaigns/"+strconv.Itoa(id), nil, nil, "", &out)
	return out, err
}

type ContactsService struct{ c *Client }

func (s *ContactsService) Upsert(ctx context.Context, body map[string]any) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "POST", "/api/v1/contacts", nil, body, "", &out)
	return out, err
}

func (s *ContactsService) List(ctx context.Context, filters map[string]string) (map[string]any, error) {
	q := url.Values{}
	for k, v := range filters {
		q.Set(k, v)
	}
	var out map[string]any
	err := s.c.do(ctx, "GET", "/api/v1/contacts", q, nil, "", &out)
	return out, err
}

type EventsService struct{ c *Client }

func (s *EventsService) Ingest(ctx context.Context, body map[string]any) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "POST", "/api/v1/events", nil, body, "", &out)
	return out, err
}

type WorkflowsService struct{ c *Client }

func (s *WorkflowsService) List(ctx context.Context) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "GET", "/api/v1/workflows", nil, nil, "", &out)
	return out, err
}

func (s *WorkflowsService) Create(ctx context.Context, body map[string]any) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "POST", "/api/v1/workflows", nil, body, "", &out)
	return out, err
}

func (s *WorkflowsService) Publish(ctx context.Context, slug string) (map[string]any, error) {
	var out map[string]any
	err := s.c.do(ctx, "POST", fmt.Sprintf("/api/v1/workflows/%s/publish", slug), nil, map[string]any{}, "", &out)
	return out, err
}

type RequestLogsService struct{ c *Client }

func (s *RequestLogsService) List(ctx context.Context, filters map[string]string) (map[string]any, error) {
	q := url.Values{}
	for k, v := range filters {
		q.Set(k, v)
	}
	var out map[string]any
	err := s.c.do(ctx, "GET", "/api/v1/request-logs", q, nil, "", &out)
	return out, err
}
