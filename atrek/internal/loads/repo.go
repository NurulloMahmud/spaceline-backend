package loads

import (
	"encoding/json"
	"strings"
	"time"

	"gorm.io/datatypes"
	"gorm.io/gorm"

	"github.com/makhammatovb/atrek/internal/utils"
	"github.com/makhammatovb/atrek/pkg/database"
)

type Repository struct {
	db *gorm.DB
}

func NewRepository(db *gorm.DB) *Repository {
	return &Repository{db: db}
}

func (r *Repository) SaveRawEvent(eventType, label string, raw json.RawMessage) error {
	event := database.LoadEvent{
		Type:    eventType,
		Label:   label,
		RawData: datatypes.JSON(raw),
	}
	return r.db.Create(&event).Error
}

func (r *Repository) SaveLoad(data LoadCreatedData) (string, error) {
	vehicleTeamJSON, _ := json.Marshal(data.VehicleTeam)
	usersUUIDJSON, _ := json.Marshal(data.UsersUUID)

	load := database.Load{
		LoadID: data.ID,

		PickUpAt:      data.PickUpAt,
		PickUpAtState: data.PickUpAtState,
		PickUpZip:     data.PickUpZip,
		PickUpLat:     data.PickUpLat,
		PickUpLng:     data.PickUpLng,
		PickUpDate:    parseTimePtr(data.PickUpDate),

		DeliverTo:      data.DeliverTo,
		DeliverToState: data.DeliverToState,
		DeliverZip:     data.DeliverZip,
		DeliveryDate:   parseTimePtr(data.DeliveryDate),

		SourceID:   data.SourceID,
		SourceName: data.SourceName,
		CompanyID:  data.CompanyID,

		ContactName:    data.ContactName,
		ReceivedDate:   parseTimePtr(data.ReceivedDate),
		SuggestedTruck: data.SuggestedTruck,
		VehicleType:    data.VehicleType,
		Miles:          data.Miles,
		MilesOut:       data.MilesOut,

		NearestVehiclesCount: data.NearestVehiclesCount,
		BrokerCompany:        data.BrokerCompany,
		BrokerRating:         data.BrokerRating,
		VehicleTeam:          datatypes.JSON(vehicleTeamJSON),
		UsersUUID:            datatypes.JSON(usersUUIDJSON),

		CountDay:    data.CountDay,
		IsDriverBid: data.IsDriverBid,
		IsRead:      data.IsRead,
		IsBid:       data.IsBid,
	}

	if err := r.db.Create(&load).Error; err != nil {
		return "", err
	}

	return strings.ToLower(load.ID.String()), nil
}

func (r *Repository) List(filter utils.Filter) ([]database.Load, int64, error) {
	var loads []database.Load
	var total int64
	query := r.db.Model(&database.Load{})
	if filter.Search != "" {
		search := "%" + filter.Search + "%"
		query = query.Where(
			`pick_up_at ILIKE ? 
			OR pick_up_at_state ILIKE ? 
			OR pick_up_zip ILIKE ? 
			OR deliver_to ILIKE ? 
			OR deliver_to_state ILIKE ? 
			OR deliver_zip ILIKE ?`,
			search, search, search, search, search, search,
		)
	}

	if err := query.Count(&total).Error; err != nil {
		return nil, 0, err
	}

	allowedSortFields := map[string]bool{
		"created_at":    true,
		"miles":         true,
		"received_date": true,
		"pick_up_date":  true,
	}
	sortField := "created_at"
	sortDir := "DESC"
	if filter.Sort != "" {
		if filter.Sort[0] == '-' {
			if allowedSortFields[filter.Sort[1:]] {
				sortField = filter.Sort[1:]
				sortDir = "DESC"
			}
		} else {
			if allowedSortFields[filter.Sort] {
				sortField = filter.Sort
				sortDir = "ASC"
			}
		}
	}

	offset := (filter.Page - 1) * filter.Limit
	err := query.
		Order(sortField + " " + sortDir).
		Offset(offset).
		Limit(filter.Limit).
		Find(&loads).Error
	if err != nil {
		return nil, 0, err
	}
	return loads, total, nil
}

func parseTimePtr(s string) *time.Time {
	if s == "" {
		return nil
	}
	t, err := time.Parse(time.RFC3339, s)
	if err != nil {
		return nil
	}
	return &t
}

func (r *Repository) FindByID(id string) (*database.Load, error) {
	var load database.Load
	if err := r.db.Where("id = ?", id).First(&load).Error; err != nil {
		return nil, err
	}
	return &load, nil
}

func (r *Repository) FindByUUIDs(ids []string) ([]database.Load, error) {
    var loads []database.Load
    err := r.db.Where("id IN ?", ids).Find(&loads).Error
    return loads, err
}
