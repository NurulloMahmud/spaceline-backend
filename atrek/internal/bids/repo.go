package bids

import (
	"time"

	"github.com/makhammatovb/atrek/pkg/database"
	"gorm.io/gorm"
)

type Repository struct {
	db *gorm.DB
}

func NewRepository(db *gorm.DB) *Repository {
	return &Repository{db: db}
}

func (r *Repository) Create(bid *database.LoadBid) error {
	return r.db.Create(bid).Error
}

func (r *Repository) GetByLoadID(loadID string, companyID int) ([]database.LoadBid, error) {
	var bids []database.LoadBid
	err := r.db.
		Where("load_id = ?", loadID).
		Where("company_id = ?", companyID).
		Order("created_at DESC").
		Find(&bids).Error
	return bids, err
}

func (r *Repository) ResolveColor(loadID string, companyID int) (database.LoadColor, error) {
	var actions []string
	err := r.db.Model(&database.LoadBid{}).
		Where("load_id = ?", loadID).
		Where("company_id = ?", companyID).
		Pluck("action", &actions).Error
	if err != nil {
		return database.ColorWhite, err
	}

	hasDispatcherBid := false
	hasDriverBid := false
	hasViewed := false

	for _, action := range actions {
		switch action {
		case "dispatcher_bid":
			hasDispatcherBid = true
		case "driver_bid":
			hasDriverBid = true
		case "viewed":
			hasViewed = true
		}
	}

	switch {
	case hasDispatcherBid:
		return database.ColorGreen, nil
	case hasDriverBid:
		return database.ColorRed, nil
	case hasViewed:
		return database.ColorGrey, nil
	default:
		return database.ColorWhite, nil
	}
}

func (r *Repository) HasDispatcherBid(loadID string) (bool, error) {
	var count int64
	err := r.db.Model(&database.LoadBid{}).
		Where("load_id = ? AND action = ?", loadID, "dispatcher_bid").
		Count(&count).Error
	return count > 0, err
}

func (r *Repository) ResolveColorString(loadID string, companyID int) (string, error) {
	color, err := r.ResolveColor(loadID, companyID)
	return string(color), err
}

func (r *Repository) GetByDriverID(driverID int) ([]database.LoadBid, error) {
	var bids []database.LoadBid
	err := r.db.
		Where("driver_id = ?", driverID).
		Order("created_at DESC").
		Find(&bids).Error
	return bids, err
}

func (r *Repository) GetByCompanyID(companyID int, page, limit int, f BidListFilter) ([]database.LoadBid, int64, error) {
	var bids []database.LoadBid
	var total int64

	query := r.db.Model(&database.LoadBid{}).
		Where("load_bids.company_id = ? AND load_bids.action = ?", companyID, f.Action)

	if f.Ignored != nil {
		if *f.Ignored {
			query = query.Where("load_bids.ignored_at IS NOT NULL")
		} else {
			query = query.Where("load_bids.ignored_at IS NULL")
		}
	}

	if f.PickUpState != "" || f.DeliverState != "" || f.Brokerage != "" {
		query = query.Joins("JOIN loads ON loads.id = load_bids.load_id::uuid")
		if f.PickUpState != "" {
			query = query.Where("loads.pick_up_at_state ILIKE ?", "%"+f.PickUpState+"%")
		}
		if f.DeliverState != "" {
			query = query.Where("loads.deliver_to_state ILIKE ?", "%"+f.DeliverState+"%")
		}
		if f.Brokerage != "" {
			query = query.Where("loads.contact_name ILIKE ?", "%"+f.Brokerage+"%")
		}
	}

	if f.BidPlaced != nil {
		exists := `SELECT 1 FROM load_bids placed
		           WHERE placed.load_id = load_bids.load_id
		             AND placed.company_id = load_bids.company_id
		             AND placed.action = 'dispatcher_bid'`
		if *f.BidPlaced {
			query = query.Where("EXISTS (" + exists + ")")
		} else {
			query = query.Where("NOT EXISTS (" + exists + ")")
		}
	}

	query.Count(&total)
	err := query.
		Order("load_bids.created_at DESC").
		Offset((page - 1) * limit).
		Limit(limit).
		Find(&bids).Error
	return bids, total, err
}

func (r *Repository) GetByID(bidID string) (*database.LoadBid, error) {
	var bid database.LoadBid
	if err := r.db.Where("id = ?", bidID).First(&bid).Error; err != nil {
		return nil, err
	}
	return &bid, nil
}

func (r *Repository) SetIgnored(bidID string, userID *int64, ignored bool) error {
	values := map[string]interface{}{
		"ignored_at": nil,
		"ignored_by": nil,
	}
	if ignored {
		now := time.Now()
		values["ignored_at"] = &now
		values["ignored_by"] = userID
	}
	return r.db.Model(&database.LoadBid{}).
		Where("id = ?", bidID).
		Updates(values).Error
}

// LoadsWithDispatcherBid reports which of these loads this company has already
// bid to a broker, so the board can mark them rather than only hide them.
func (r *Repository) LoadsWithDispatcherBid(loadIDs []string, companyID int) (map[string]bool, error) {
	result := make(map[string]bool)
	if len(loadIDs) == 0 {
		return result, nil
	}

	var ids []string
	err := r.db.Model(&database.LoadBid{}).
		Where("load_id IN ? AND company_id = ? AND action = ?", loadIDs, companyID, "dispatcher_bid").
		Distinct().
		Pluck("load_id", &ids).Error
	if err != nil {
		return result, err
	}

	for _, id := range ids {
		result[id] = true
	}
	return result, nil
}

func (r *Repository) GetDriverBidAmounts(loadIDs []string) (map[string]*float64, error) {
	var bids []database.LoadBid
	err := r.db.
		Where("load_id IN ? AND action = ?", loadIDs, "driver_bid").
		Find(&bids).Error
	if err != nil {
		return nil, err
	}

	result := make(map[string]*float64)
	for _, b := range bids {
		if b.BidAmount != nil {
			amount := *b.BidAmount
			result[b.LoadID] = &amount
		}
	}
	return result, nil
}
