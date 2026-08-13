package loads

import (
	"encoding/json"
	"time"

	"github.com/makhammatovb/atrek/internal/utils"
)

type LoadCreatedData struct {
	ID             int     `json:"id"`
	PickUpAt       string  `json:"pick_up_at"`
	DeliverTo      string  `json:"deliver_to"`
	SourceID       int     `json:"source_id"`
	ContactName    string  `json:"contact_name"`
	ReceivedDate   string  `json:"received_date"`
	SuggestedTruck string  `json:"suggested_truck"`
	Miles          int     `json:"miles"`
	SourceName     string  `json:"source_name"`
	CompanyID      string  `json:"company_id"`
	PickUpAtState  string  `json:"pick_up_at_state"`
	PickUpDate     string  `json:"pick_up_date"`
	PickUpZip      string  `json:"pick_up_zip"`
	PickUpLat      float64 `json:"pick_up_latitude"`
	PickUpLng      float64 `json:"pick_up_longitude"`

	DeliverToState string `json:"deliver_to_state"`
	DeliveryDate   string `json:"delivery_date"`
	DeliverZip     string `json:"deliver_zip"`

	VehicleType string `json:"vehicle_type"`

	UsersUUID []string `json:"users_uuid"`

	MilesOut             int   `json:"miles_out"`
	NearestVehiclesCount int   `json:"nearest_vehicles_count"`
	BrokerCompany        int   `json:"broker_company"`
	BrokerRating         int   `json:"broker_rating"`
	VehicleTeam          []int `json:"vehicle_team"`

	CountDay    int  `json:"count_day"`
	IsDriverBid bool `json:"is_driver_bid"`
	IsRead      bool `json:"is_read"`
	IsBid       bool `json:"is_bid"`
}

type StreamMessage struct {
    ID             string          `json:"id"`
    Type           string          `json:"type"`
    Label          string          `json:"label"`
    Data           json.RawMessage `json:"data"`
    Timestamp      time.Time       `json:"timestamp"`
    PickUpLat      float64         `json:"pick_up_lat"`
    PickUpLng      float64         `json:"pick_up_lng"`
    PickUpAt       string          `json:"pick_up_at"`
    PickUpAtState  string          `json:"pick_up_at_state"`
    DeliverTo      string          `json:"deliver_to"`
    DeliverToState string          `json:"deliver_to_state"`
    VehicleType    string          `json:"vehicle_type"`
    Miles          int             `json:"miles"`
    ContactName    string          `json:"contact_name"`
}

type LoadListItem struct {
	ID            string     `json:"id"`
	LoadID        int        `json:"load_id"`
	PickUpAt      string     `json:"pick_up_at"`
	PickUpDate    *time.Time `json:"pick_up_date"`
	DeliverTo     string     `json:"deliver_to"`
	DeliveryDate  *time.Time `json:"delivery_date"`
	VehicleType   string     `json:"vehicle_type"`
	Miles         int        `json:"miles"`
	SourceName    string     `json:"source_name"`
	BrokerCompany int        `json:"broker_company"`
	BrokerRating  int        `json:"broker_rating"`
	ContactName   string     `json:"contact_name"`
	Color         string     `json:"color"`
	ReceivedDate  *time.Time `json:"received_date"`
	CreatedAt     time.Time  `json:"created_at"`
}

type LoadListResponse struct {
	Items    []LoadListItem `json:"items"`
	Metadata utils.Metadata `json:"metadata"`
}
