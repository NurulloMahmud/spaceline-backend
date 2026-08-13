package database

import (
	"time"

	"github.com/google/uuid"
	"gorm.io/datatypes"
)

type Model struct {
	ID        uuid.UUID `gorm:"type:uuid;primaryKey;default:gen_random_uuid()" json:"id"`
	CreatedAt time.Time `gorm:"type:timestamptz;not null;default:now()" json:"-"`
	UpdatedAt time.Time `gorm:"type:timestamptz;not null;default:now()" json:"-"`
}

type LoadEvent struct {
	Model
	Type      string         `gorm:"index;not null" json:"type"`
	Label     string         `json:"label"`
	RawData   datatypes.JSON `gorm:"type:jsonb" json:"raw_data"`
	CreatedAt time.Time      `json:"created_at"`
}

func (LoadEvent) TableName() string {
	return "load_events"
}

type Load struct {
	Model
	LoadID int `gorm:"column:load_id;index" json:"load_id"`

	PickUpAt      string     `json:"pick_up_at"`
	PickUpAtState string     `json:"pick_up_at_state"`
	PickUpZip     string     `json:"pick_up_zip"`
	PickUpLat     float64    `gorm:"column:pick_up_latitude" json:"pick_up_latitude"`
	PickUpLng     float64    `gorm:"column:pick_up_longitude" json:"pick_up_longitude"`
	PickUpDate    *time.Time `json:"pick_up_date"`

	DeliverTo      string     `json:"deliver_to"`
	DeliverToState string     `json:"deliver_to_state"`
	DeliverZip     string     `json:"deliver_zip"`
	DeliveryDate   *time.Time `json:"delivery_date"`

	SourceID   int    `json:"source_id"`
	SourceName string `json:"source_name"`
	CompanyID  string `json:"company_id"`

	ContactName    string     `json:"contact_name"`
	ReceivedDate   *time.Time `json:"received_date"`
	SuggestedTruck string     `json:"suggested_truck"`
	VehicleType    string     `json:"vehicle_type"`
	Miles          int        `json:"miles"`
	MilesOut       int        `json:"miles_out"`

	NearestVehiclesCount int            `json:"nearest_vehicles_count"`
	BrokerCompany        int            `json:"broker_company"`
	BrokerRating         int            `json:"broker_rating"`
	VehicleTeam          datatypes.JSON `gorm:"type:jsonb" json:"vehicle_team"`
	UsersUUID            datatypes.JSON `gorm:"type:jsonb" json:"users_uuid"`

	CountDay    int  `json:"count_day"`
	IsDriverBid bool `json:"is_driver_bid"`
	IsRead      bool `json:"is_read"`
	IsBid       bool `json:"is_bid"`
}

func (Load) TableName() string {
	return "loads"
}

type LoadBid struct {
    Model
    LoadID       string   `gorm:"type:uuid;index;not null" json:"load_id"`
    DriverID     *int     `gorm:"index" json:"driver_id"`
    UserID       *int64   `gorm:"index" json:"user_id"`
    CompanyID    int      `gorm:"index;not null" json:"company_id"`
    Action       string   `gorm:"not null" json:"action"`
    BidAmount    *float64 `json:"bid_amount"`
    DriverAmount *float64 `json:"driver_amount"`
    Note         *string  `json:"note"`

    // Dispatchers can ignore a driver bid to move it out of the bid board's
    // main focus without deleting it.
    IgnoredAt *time.Time `gorm:"index" json:"ignored_at"`
    IgnoredBy *int64     `json:"ignored_by"`
}

func (LoadBid) TableName() string {
	return "load_bids"
}

type LoadColor string

const (
    ColorWhite LoadColor = "white"
    ColorGrey  LoadColor = "grey"
    ColorRed   LoadColor = "red"
    ColorGreen LoadColor = "green"
)
