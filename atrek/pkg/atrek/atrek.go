package atrek

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

type RawEnvelope struct {
	Data  json.RawMessage `json:"data"`
	Label string          `json:"label"`
	Type  string          `json:"type"`
}

type Event struct {
	Type       string
	Label      string
	Raw        json.RawMessage
	ReceivedAt time.Time
}

type Client struct {
	url            string
	events         chan Event
	reconnectDelay time.Duration
}

func NewClient(wsURL string) *Client {
	return &Client{
		url:            wsURL,
		events:         make(chan Event, 256),
		reconnectDelay: 3 * time.Second,
	}
}

func (c *Client) Events() <-chan Event {
	return c.events
}

func (c *Client) Run() {
	for {
		if err := c.connectAndListen(); err != nil {
			log.Printf("atrek: connection error: %v (retrying in %s)", err, c.reconnectDelay)
		} else {
			log.Printf("atrek: connection closed cleanly (retrying in %s)", c.reconnectDelay)
		}
		time.Sleep(c.reconnectDelay)
	}
}

func (c *Client) connectAndListen() error {
	u, err := url.Parse(c.url)
	if err != nil {
		return err
	}

	conn, _, err := websocket.DefaultDialer.Dial(u.String(), nil)
	if err != nil {
		return err
	}
	defer conn.Close()
	log.Println("atrek: connected")

	for {
		_, message, err := conn.ReadMessage()
		if err != nil {
			return err
		}

		var envelope RawEnvelope
		if err := json.Unmarshal(message, &envelope); err != nil {
			log.Printf("atrek: failed to decode envelope: %v", err)
			continue
		}

		rawData := envelope.Data
		var inner string
		if err := json.Unmarshal(envelope.Data, &inner); err == nil {
			rawData = json.RawMessage(inner)
		}

		c.events <- Event{
			Type:       envelope.Type,
			Label:      envelope.Label,
			Raw:        rawData,
			ReceivedAt: time.Now(),
		}
	}
}

type loginRequest struct {
	Username string `json:"username"`
	Password string `json:"password"`
}

type loginResponse struct {
	Access  string `json:"access"`
	Refresh string `json:"refresh"`
}

type HTTPClient struct {
	baseURL    string
	username   string
	password   string
	httpClient *http.Client

	mu       sync.Mutex
	token    string
	tokenExp time.Time
}

func NewHTTPClient(baseURL, username, password string) *HTTPClient {
	return &HTTPClient{
		baseURL:    baseURL,
		username:   username,
		password:   password,
		httpClient: &http.Client{Timeout: 30 * time.Second},
	}
}

func (c *HTTPClient) login() error {
	body, err := json.Marshal(loginRequest{
		Username: c.username,
		Password: c.password,
	})
	if err != nil {
		return err
	}

	resp, err := c.httpClient.Post(
		c.baseURL+"/app/api/user/login/",
		"application/json",
		bytes.NewReader(body),
	)
	if err != nil {
		return fmt.Errorf("atrek: login request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("atrek: login returned status %d", resp.StatusCode)
	}

	var result loginResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return fmt.Errorf("atrek: login decode failed: %w", err)
	}
	if result.Access == "" {
		return fmt.Errorf("atrek: login returned empty access token")
	}

	c.token = result.Access
	c.tokenExp = time.Now().Add(12 * time.Hour)

	log.Println("atrek: authenticated successfully")
	return nil
}

func (c *HTTPClient) getToken() (string, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.token != "" && time.Now().Before(c.tokenExp.Add(-5*time.Minute)) {
		return c.token, nil
	}

	if err := c.login(); err != nil {
		return "", err
	}

	return c.token, nil
}

func (c *HTTPClient) GetLoad(loadID int) (json.RawMessage, error) {
	token, err := c.getToken()
	if err != nil {
		return nil, err
	}

	req, err := http.NewRequest(
		http.MethodGet,
		fmt.Sprintf("%s/app/api/load/%d/", c.baseURL, loadID),
		nil,
	)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+token)

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("atrek: get load request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusUnauthorized {
		log.Println("atrek: token expired, re-authenticating...")
		c.mu.Lock()
		c.token = ""
		c.mu.Unlock()
		return c.GetLoad(loadID)
	}

	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("atrek: get load returned status %d", resp.StatusCode)
	}

	var raw json.RawMessage
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return nil, fmt.Errorf("atrek: get load decode failed: %w", err)
	}

	return raw, nil
}
