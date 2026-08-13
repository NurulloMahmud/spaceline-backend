package sse

import (
	"log"
	"sync"
)

type Hub[T any] struct {
	mu          sync.RWMutex
	subscribers map[chan T]bool
	bufferSize int
}

func NewHub[T any](bufferSize int) *Hub[T] {
	if bufferSize <= 0 {
		bufferSize = 32
	}
	return &Hub[T]{
		subscribers: make(map[chan T]bool),
		bufferSize:  bufferSize,
	}
}

func (h *Hub[T]) Subscribe() chan T {
	ch := make(chan T, h.bufferSize)
	h.mu.Lock()
	h.subscribers[ch] = true
	h.mu.Unlock()
	return ch
}

func (h *Hub[T]) Unsubscribe(ch chan T) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.subscribers[ch]; ok {
		delete(h.subscribers, ch)
		close(ch)
	}
}

func (h *Hub[T]) Broadcast(msg T) {
	h.mu.RLock()
	defer h.mu.RUnlock()

	for ch := range h.subscribers {
		select {
		case ch <- msg:
		default:
			log.Println("sse: dropping message for slow subscriber")
		}
	}
}

func (h *Hub[T]) SubscriberCount() int {
	h.mu.RLock()
	defer h.mu.RUnlock()
	return len(h.subscribers)
}
