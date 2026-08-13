package bids

import "github.com/makhammatovb/atrek/pkg/sse"

type BidMessage struct {
	Type      string  `json:"type"`
	LoadID    string  `json:"load_id"`
	CompanyID int     `json:"company_id"`
	Action    string  `json:"action"`
	Color     string  `json:"color"`
	Amount    float64 `json:"amount"`
	BidID     string  `json:"bid_id,omitempty"`
}

type Hub struct {
	hub *sse.Hub[BidMessage]
}

func NewHub() *Hub {
	return &Hub{hub: sse.NewHub[BidMessage](32)}
}

func (h *Hub) Broadcast(msg interface{}) {
	if m, ok := msg.(BidMessage); ok {
		h.hub.Broadcast(m)
	}
}

func (h *Hub) Subscribe() chan BidMessage {
	return h.hub.Subscribe()
}

func (h *Hub) Unsubscribe(ch chan BidMessage) {
	h.hub.Unsubscribe(ch)
}
