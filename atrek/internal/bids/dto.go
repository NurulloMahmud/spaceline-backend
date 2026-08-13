package bids

import (
	"time"

	"github.com/makhammatovb/atrek/pkg/database"
)

type LoadSummary struct {
	ID           string     `json:"id"`
	LoadID       int        `json:"load_id"`
	PickUpAt     string     `json:"pick_up_at"`
	DeliverTo    string     `json:"deliver_to"`
	VehicleType  string     `json:"vehicle_type"`
	Miles        int        `json:"miles"`
	SourceName   string     `json:"source_name"`
	ContactName  string     `json:"contact_name"`
	PickUpDate   *time.Time `json:"pick_up_date"`
	DeliveryDate *time.Time `json:"delivery_date"`
}

type BidResponse struct {
	ID           string      `json:"id"`
	Action       string      `json:"action"`
	BidAmount    *float64    `json:"bid_amount"`
	DriverAmount *float64    `json:"driver_amount"`
	Note         *string     `json:"note"`
	CreatedAt    interface{} `json:"created_at"`
	User         *UserInfo   `json:"user"`
	Driver       *DriverInfo `json:"driver"`
}

type DriverBidResponse struct {
	ID        string       `json:"id"`
	Action    string       `json:"action"`
	BidAmount *float64     `json:"bid_amount"`
	Note      *string      `json:"note"`
	CreatedAt interface{}  `json:"created_at"`
	Load      *LoadSummary `json:"load"`
}

func ToLoadSummary(l database.Load) *LoadSummary {
	return &LoadSummary{
		ID:           l.ID.String(),
		LoadID:       l.LoadID,
		PickUpAt:     l.PickUpAt,
		DeliverTo:    l.DeliverTo,
		VehicleType:  l.VehicleType,
		Miles:        l.Miles,
		SourceName:   l.SourceName,
		ContactName:  l.ContactName,
		PickUpDate:   l.PickUpDate,
		DeliveryDate: l.DeliveryDate,
	}
}

func ToBidResponse(b database.LoadBid, users map[int64]UserInfo, drivers map[int]DriverInfo) BidResponse {
	item := BidResponse{
		ID:           b.ID.String(),
		Action:       b.Action,
		BidAmount:    b.BidAmount,
		DriverAmount: b.DriverAmount,
		Note:         b.Note,
		CreatedAt:    b.CreatedAt,
	}
	if b.UserID != nil {
		if u, ok := users[*b.UserID]; ok {
			item.User = &u
		}
	}
	if b.DriverID != nil {
		if d, ok := drivers[*b.DriverID]; ok {
			item.Driver = &d
		}
	}
	return item
}

func ToDriverBidResponse(b database.LoadBid, loads map[string]database.Load) DriverBidResponse {
	item := DriverBidResponse{
		ID:        b.ID.String(),
		Action:    b.Action,
		BidAmount: b.BidAmount,
		Note:      b.Note,
		CreatedAt: b.CreatedAt,
	}
	if l, ok := loads[b.LoadID]; ok {
		item.Load = ToLoadSummary(l)
	}
	return item
}

type CompanyBidResponse struct {
	ID           string       `json:"id"`
	Action       string       `json:"action"`
	CreatedAt    interface{}  `json:"created_at"`
	BidAmount    *float64     `json:"bid_amount"`
	DriverAmount *float64     `json:"driver_amount"`
	Note         *string      `json:"note"`
	Dispatcher   *UserInfo    `json:"dispatcher"`
	Driver       *DriverInfo  `json:"driver"`
	DriverBid    *float64     `json:"driver_bid"`
	Load         *LoadSummary `json:"load"`
	Ignored      bool         `json:"ignored"`
	IgnoredAt    *time.Time   `json:"ignored_at"`
	// BidPlaced is true when this company has already bid this load to the
	// broker. Such rows are hidden from the board unless bid_placed=true.
	BidPlaced bool `json:"bid_placed"`
}

func ToCompanyBidResponse(
	b database.LoadBid,
	users map[int64]UserInfo,
	drivers map[int]DriverInfo,
	driverBids map[string]*float64,
	loads map[string]database.Load,
	bidPlaced map[string]bool,
) CompanyBidResponse {
	item := CompanyBidResponse{
		ID:           b.ID.String(),
		Action:       b.Action,
		CreatedAt:    b.CreatedAt,
		BidAmount:    b.BidAmount,
		DriverAmount: b.DriverAmount,
		Note:         b.Note,
		Ignored:      b.IgnoredAt != nil,
		IgnoredAt:    b.IgnoredAt,
		BidPlaced:    bidPlaced[b.LoadID],
	}
	if b.UserID != nil {
		if u, ok := users[*b.UserID]; ok {
			item.Dispatcher = &u
		}
	}
	if b.DriverID != nil {
		if d, ok := drivers[*b.DriverID]; ok {
			item.Driver = &d
		}
	}
	if amount, ok := driverBids[b.LoadID]; ok {
		item.DriverBid = amount
	}
	if l, ok := loads[b.LoadID]; ok {
		item.Load = ToLoadSummary(l)
	}
	return item
}
