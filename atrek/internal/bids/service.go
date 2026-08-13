package bids

import (
	"errors"

	"github.com/makhammatovb/atrek/pkg/database"
)

var ErrForbidden = errors.New("bid belongs to another company")

type SSEBroadcaster interface {
	Broadcast(msg interface{})
}

type Service struct {
	repo        *Repository
	loadRepo    LoadRepository
	broadcaster SSEBroadcaster
}

type LoadRepository interface {
	FindByUUIDs(ids []string) ([]database.Load, error)
}

func NewService(repo *Repository, loadRepo LoadRepository, broadcaster SSEBroadcaster) *Service {
	return &Service{repo: repo, loadRepo: loadRepo, broadcaster: broadcaster}
}

func (s *Service) Subscribe() chan BidMessage {
	return s.broadcaster.(*Hub).Subscribe()
}

func (s *Service) Unsubscribe(ch chan BidMessage) {
	s.broadcaster.(*Hub).Unsubscribe(ch)
}

func (s *Service) RecordView(loadID string, userID *int64, driverID *int, companyID int) error {
	bid := &database.LoadBid{
		LoadID:    loadID,
		UserID:    userID,
		DriverID:  driverID,
		CompanyID: companyID,
		Action:    "viewed",
	}
	return s.repo.Create(bid)
}

func (s *Service) RecordBid(loadID string, userID *int64, driverID *int, companyID int, amount float64, driverAmount *float64, note string) error {
	action := "dispatcher_bid"
	if userID == nil {
		action = "driver_bid"
	}
	bid := &database.LoadBid{
		LoadID:       loadID,
		UserID:       userID,
		DriverID:     driverID,
		CompanyID:    companyID,
		Action:       action,
		BidAmount:    &amount,
		DriverAmount: driverAmount,
		Note:         &note,
	}
	if err := s.repo.Create(bid); err != nil {
		return err
	}
	color, _ := s.repo.ResolveColor(loadID, companyID)
	s.broadcaster.Broadcast(BidMessage{
		Type:      "bid",
		LoadID:    loadID,
		CompanyID: companyID,
		Action:    action,
		Color:     string(color),
		Amount:    amount,
		BidID:     bid.ID.String(),
	})
	return nil
}

// SetBidIgnored moves a bid in or out of the bid board's main focus. It returns
// the bid so callers can verify company ownership before acting.
func (s *Service) SetBidIgnored(bidID string, companyID int, userID *int64, ignored bool) (*database.LoadBid, error) {
	bid, err := s.repo.GetByID(bidID)
	if err != nil {
		return nil, err
	}
	if bid.CompanyID != companyID {
		return nil, ErrForbidden
	}
	if err := s.repo.SetIgnored(bidID, userID, ignored); err != nil {
		return nil, err
	}

	msgType := "bid_ignored"
	if !ignored {
		msgType = "bid_unignored"
	}
	color, _ := s.repo.ResolveColor(bid.LoadID, companyID)
	amount := 0.0
	if bid.BidAmount != nil {
		amount = *bid.BidAmount
	}
	s.broadcaster.Broadcast(BidMessage{
		Type:      msgType,
		LoadID:    bid.LoadID,
		CompanyID: companyID,
		Action:    bid.Action,
		Color:     string(color),
		Amount:    amount,
		BidID:     bid.ID.String(),
	})

	return bid, nil
}

func (s *Service) GetBids(loadID string, companyID int) ([]database.LoadBid, database.LoadColor, error) {
	bids, err := s.repo.GetByLoadID(loadID, companyID)
	if err != nil {
		return nil, database.ColorWhite, err
	}
	color, err := s.repo.ResolveColor(loadID, companyID)
	return bids, color, err
}

func (s *Service) GetDriverBids(driverID int) ([]database.LoadBid, error) {
	return s.repo.GetByDriverID(driverID)
}

func (s *Service) GetLoadsByUUIDs(ids []string) ([]database.Load, error) {
	return s.loadRepo.FindByUUIDs(ids)
}

func (s *Service) GetCompanyBids(companyID int, pagination BidPagination, filter BidListFilter) ([]database.LoadBid, int64, error) {
    return s.repo.GetByCompanyID(companyID, pagination.Page, pagination.Limit, filter)
}

func (s *Service) GetDriverBidAmounts(loadIDs []string) (map[string]*float64, error) {
	return s.repo.GetDriverBidAmounts(loadIDs)
}

func (s *Service) LoadsWithDispatcherBid(loadIDs []string, companyID int) (map[string]bool, error) {
	return s.repo.LoadsWithDispatcherBid(loadIDs, companyID)
}
