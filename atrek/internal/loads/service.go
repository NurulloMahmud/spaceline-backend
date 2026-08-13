package loads

import (
	"encoding/json"
	"log"
	"strings"
	"time"

	"github.com/makhammatovb/atrek/internal/utils"
	"github.com/makhammatovb/atrek/pkg/atrek"
	"github.com/makhammatovb/atrek/pkg/database"
	"github.com/makhammatovb/atrek/pkg/sse"
)

type ColorResolver interface {
	ResolveColorString(loadID string, companyID int) (string, error)
}

type Service struct {
	repo          *Repository
	hub           *sse.Hub[StreamMessage]
	atrekClient   *atrek.HTTPClient
	colorResolver ColorResolver
}

func NewService(repo *Repository, atrekClient *atrek.HTTPClient, colorResolver ColorResolver) *Service {
	return &Service{
		repo:          repo,
		hub:           sse.NewHub[StreamMessage](32),
		atrekClient:   atrekClient,
		colorResolver: colorResolver,
	}
}

func (s *Service) Subscribe() chan StreamMessage {
	return s.hub.Subscribe()
}

func (s *Service) Unsubscribe(ch chan StreamMessage) {
	s.hub.Unsubscribe(ch)
}

func (s *Service) ConsumeAtrekEvents(events <-chan atrek.Event) {
	for ev := range events {
		s.handleEvent(ev)
	}
}

func (s *Service) handleEvent(ev atrek.Event) {
	if err := s.repo.SaveRawEvent(ev.Type, ev.Label, ev.Raw); err != nil {
		log.Printf("loads: failed to save raw event (type=%s): %v", ev.Type, err)
	}

	msg := StreamMessage{
		Type:      ev.Type,
		Label:     ev.Label,
		Data:      ev.Raw,
		Timestamp: time.Now(),
	}

	switch ev.Type {
	case "LOAD_CREATED":
		var data LoadCreatedData
		if err := json.Unmarshal(ev.Raw, &data); err != nil {
			log.Printf("loads: failed to decode LOAD_CREATED data: %v", err)
		} else {
			id, err := s.repo.SaveLoad(data)
			if err != nil {
				log.Printf("loads: failed to save structured load: %v", err)
			} else {
				msg.ID = id
				msg.PickUpLat = data.PickUpLat
				msg.PickUpLng = data.PickUpLng
				msg.PickUpAt = data.PickUpAt
				msg.PickUpAtState = data.PickUpAtState
				msg.DeliverTo = data.DeliverTo
				msg.DeliverToState = data.DeliverToState
				msg.VehicleType = data.VehicleType
				msg.Miles = data.Miles
				msg.ContactName = data.ContactName
			}
		}
	}
	s.hub.Broadcast(msg)
}

func (s *Service) ListLoads(filter utils.Filter, driverFilter DriverFilter, loadFilter LoadFilter) (LoadListResponse, error) {
	records, total, err := s.repo.List(filter)
	if err != nil {
		return LoadListResponse{}, err
	}

	items := make([]LoadListItem, 0, len(records))
	for _, r := range records {
		if driverFilter.Active && !driverFilter.Matches(r.PickUpLat, r.PickUpLng) {
			total--
			continue
		}

		proxy := StreamMessage{
			PickUpAt:       r.PickUpAt,
			PickUpAtState:  r.PickUpAtState,
			DeliverTo:      r.DeliverTo,
			DeliverToState: r.DeliverToState,
			VehicleType:    r.VehicleType,
			Miles:          r.Miles,
			ContactName:    r.ContactName,
		}
		if !loadFilter.Matches(proxy) {
			total--
			continue
		}
		color := "white"
		if s.colorResolver != nil {
			if c, err := s.colorResolver.ResolveColorString(r.ID.String(), filter.CompanyID); err == nil {
				color = c
			}
		}
		items = append(items, LoadListItem{
			ID:            strings.ToLower(r.ID.String()),
			LoadID:        r.LoadID,
			PickUpAt:      r.PickUpAt,
			PickUpDate:    r.PickUpDate,
			DeliverTo:     r.DeliverTo,
			DeliveryDate:  r.DeliveryDate,
			VehicleType:   r.VehicleType,
			Miles:         r.Miles,
			SourceName:    r.SourceName,
			BrokerCompany: r.BrokerCompany,
			BrokerRating:  r.BrokerRating,
			ContactName:   r.ContactName,
			ReceivedDate:  r.ReceivedDate,
			CreatedAt:     r.CreatedAt,
			Color:         color,
		})
	}
	metadata := utils.CalculateMetadata(total, int64(filter.Page), int64(filter.Limit))
	return LoadListResponse{Items: items, Metadata: metadata}, nil
}

func (s *Service) RetrieveLoad(id string) (json.RawMessage, error) {
	load, err := s.repo.FindByID(id)
	if err != nil {
		return nil, utils.ErrNotFound
	}

	data, err := s.atrekClient.GetLoad(load.LoadID)
	if err != nil {
		return nil, utils.ErrAtrekFetch
	}

	return mergeStoredFields(data, load), nil
}

// The upstream load detail carries dimensions but no geography, while the
// feed event we stored carries geography but no dimensions. Consumers need
// both — deadhead cannot be computed without pickup coordinates — so the
// stored fields are folded in wherever the detail leaves them out.
func mergeStoredFields(data json.RawMessage, load *database.Load) json.RawMessage {
	var payload map[string]interface{}
	if err := json.Unmarshal(data, &payload); err != nil {
		log.Printf("loads: could not merge stored fields into detail: %v", err)
		return data
	}

	stored := map[string]interface{}{
		"id":                load.ID.String(),
		"pick_up_latitude":  load.PickUpLat,
		"pick_up_longitude": load.PickUpLng,
		"pick_up_zip":       load.PickUpZip,
		"pick_up_at":        load.PickUpAt,
		"pick_up_at_state":  load.PickUpAtState,
		"deliver_zip":       load.DeliverZip,
		"deliver_to":        load.DeliverTo,
		"deliver_to_state":  load.DeliverToState,
		"miles":             load.Miles,
		"contact_name":      load.ContactName,
	}
	if load.PickUpDate != nil {
		stored["pick_up_date"] = load.PickUpDate
	}
	if load.DeliveryDate != nil {
		stored["delivery_date"] = load.DeliveryDate
	}

	for key, value := range stored {
		if existing, present := payload[key]; !present || isEmpty(existing) {
			payload[key] = value
		}
	}

	merged, err := json.Marshal(payload)
	if err != nil {
		return data
	}
	return merged
}

func isEmpty(v interface{}) bool {
	switch value := v.(type) {
	case nil:
		return true
	case string:
		return value == ""
	case float64:
		return value == 0
	}
	return false
}
