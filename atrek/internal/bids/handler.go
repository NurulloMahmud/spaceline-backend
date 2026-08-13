package bids

import (
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"gorm.io/gorm"

	"github.com/makhammatovb/atrek/internal/middleware"
	"github.com/makhammatovb/atrek/internal/utils"
	"github.com/makhammatovb/atrek/pkg/database"
	auth "github.com/makhammatovb/atrek/pkg/jwt"
)

type Handler struct {
	service  *Service
	resolver *Resolver
}

func NewHandler(service *Service, resolver *Resolver) *Handler {
	return &Handler{service: service, resolver: resolver}
}

func (h *Handler) View(c *gin.Context) {
    loadID    := c.Param("id")
    companyID := GetCompanyID(c)

    log.Printf("View: loadID=%s companyID=%d", loadID, companyID)

    var userID *int64
    var driverID *int

    if val, ok := c.Get(string(middleware.UserContextKey)); ok {
        claims := val.(*auth.TokenClaims)
        userID = &claims.ID
        log.Printf("View: userID=%d", *userID)
    }
    if val, ok := c.Get(string(middleware.DriverContextKey)); ok {
        did := val.(int)
        driverID = &did
        log.Printf("View: driverID=%d", *driverID)
    }

    if err := h.service.RecordView(loadID, userID, driverID, companyID); err != nil {
        log.Printf("RecordView error: %v", err)
        c.JSON(http.StatusInternalServerError, utils.Response{Success: false, Error: "failed to record view"})
        return
    }

    c.JSON(http.StatusOK, utils.Response{Success: true})
}

func (h *Handler) Bid(c *gin.Context) {
    loadID    := c.Param("id")
    companyID := GetCompanyID(c)

    var body struct {
        DriverID     *int     `json:"driver_id"`
        UserID       *int64   `json:"user_id"`
        CompanyID    *int     `json:"company_id"`
        Amount       float64  `json:"amount" binding:"required"`
        DriverAmount *float64 `json:"driver_amount"`
        Note         string   `json:"note"`
    }
    if err := c.ShouldBindJSON(&body); err != nil {
        c.JSON(http.StatusBadRequest, utils.Response{Success: false, Error: "amount is required"})
        return
    }

    var userID *int64
    var driverID *int

    if val, ok := c.Get(string(middleware.UserContextKey)); ok {
        claims := val.(*auth.TokenClaims)
        userID = &claims.ID
        if body.DriverID == nil {
            c.JSON(http.StatusBadRequest, utils.Response{
                Success: false,
                Error:   "driver_id is required for dispatcher bid",
            })
            return
        }
        driverID = body.DriverID
    }

    if val, ok := c.Get(string(middleware.DriverContextKey)); ok {
        did := val.(int)
        driverID = &did
        body.DriverAmount = nil
    }

    // Internal callers (telegram bot, email-agent) carry no JWT identity, so the
    // actor and the company they act for come from the body instead.
    if _, internal := c.Get("is_internal"); internal {
        if body.CompanyID == nil {
            c.JSON(http.StatusBadRequest, utils.Response{
                Success: false,
                Error:   "company_id is required for internal bids",
            })
            return
        }
        companyID = *body.CompanyID
        driverID  = body.DriverID
        userID    = body.UserID
        if driverID == nil && userID == nil {
            c.JSON(http.StatusBadRequest, utils.Response{
                Success: false,
                Error:   "driver_id or user_id is required for internal bids",
            })
            return
        }
    }

    if err := h.service.RecordBid(
        loadID, userID, driverID, companyID,
        body.Amount, body.DriverAmount, body.Note,
    ); err != nil {
        c.JSON(http.StatusInternalServerError, utils.Response{Success: false, Error: "failed to record bid"})
        return
    }
    c.JSON(http.StatusCreated, utils.Response{Success: true})
}

func (h *Handler) Ignore(c *gin.Context) {
	h.setIgnored(c, true)
}

func (h *Handler) Unignore(c *gin.Context) {
	h.setIgnored(c, false)
}

func (h *Handler) setIgnored(c *gin.Context, ignored bool) {
	bidID := c.Param("bid_id")
	companyID := GetCompanyID(c)
	if companyID == 0 {
		c.JSON(http.StatusUnauthorized, utils.Response{Success: false, Error: "unauthorized"})
		return
	}

	var userID *int64
	if val, ok := c.Get(string(middleware.UserContextKey)); ok {
		claims := val.(*auth.TokenClaims)
		userID = &claims.ID
	}

	bid, err := h.service.SetBidIgnored(bidID, companyID, userID, ignored)
	if err != nil {
		switch {
		case errors.Is(err, ErrForbidden):
			c.JSON(http.StatusForbidden, utils.Response{Success: false, Error: "bid belongs to another company"})
		case errors.Is(err, gorm.ErrRecordNotFound):
			c.JSON(http.StatusNotFound, utils.Response{Success: false, Error: "bid not found"})
		default:
			log.Printf("setIgnored error: %v", err)
			c.JSON(http.StatusInternalServerError, utils.Response{Success: false, Error: "failed to update bid"})
		}
		return
	}

	c.JSON(http.StatusOK, utils.Response{
		Success: true,
		Data: map[string]interface{}{
			"id":       bid.ID.String(),
			"load_id":  bid.LoadID,
			"ignored":  ignored,
		},
	})
}

func (h *Handler) GetBids(c *gin.Context) {
	loadID := c.Param("id")
	companyID := GetCompanyID(c)
	bids, color, err := h.service.GetBids(loadID, companyID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, utils.Response{Success: false, Error: "failed to fetch bids"})
		return
	}

	var userIDs []int64
	var driverIDs []int
	seenUsers := map[int64]bool{}
	seenDrivers := map[int]bool{}

	for _, b := range bids {
		if b.UserID != nil && !seenUsers[*b.UserID] {
			userIDs = append(userIDs, *b.UserID)
			seenUsers[*b.UserID] = true
		}
		if b.DriverID != nil && !seenDrivers[*b.DriverID] {
			driverIDs = append(driverIDs, *b.DriverID)
			seenDrivers[*b.DriverID] = true
		}
	}

	users, err := h.resolver.FetchUsers(userIDs)
	if err != nil {
		log.Printf("GetBids: could not resolve users: %v", err)
	}
	drivers, err := h.resolver.FetchDrivers(driverIDs)
	if err != nil {
		log.Printf("GetBids: could not resolve drivers: %v", err)
	}
	type BidResponse struct {
		ID        string      `json:"id"`
		Action    string      `json:"action"`
		BidAmount *float64    `json:"bid_amount"`
		Note      *string     `json:"note"`
		CreatedAt interface{} `json:"created_at"`
		User      *UserInfo   `json:"user"`
		Driver    *DriverInfo `json:"driver"`
	}

	result := make([]BidResponse, 0, len(bids))
	for _, b := range bids {
		item := BidResponse{
			ID:        b.ID.String(),
			Action:    b.Action,
			BidAmount: b.BidAmount,
			Note:      b.Note,
			CreatedAt: b.CreatedAt,
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
		result = append(result, item)
	}

	c.JSON(http.StatusOK, utils.Response{
		Success: true,
		Data: map[string]interface{}{
			"color": color,
			"bids":  result,
		},
	})
}

func (h *Handler) Stream(c *gin.Context) {
	flusher, ok := c.Writer.(http.Flusher)
	if !ok {
		c.JSON(http.StatusInternalServerError, utils.Response{
			Success: false,
			Error:   "streaming unsupported",
		})
		return
	}

	companyID := GetCompanyID(c)
	c.Header("Content-Type", "text/event-stream")
	c.Header("Cache-Control", "no-cache")
	c.Header("Connection", "keep-alive")
	c.Header("X-Accel-Buffering", "no")
	ch := h.service.Subscribe()
	defer h.service.Unsubscribe(ch)
	c.SSEvent("", "connected")
	flusher.Flush()

	clientGone := c.Request.Context().Done()
	// Without traffic, nginx closes an idle upstream read at proxy_read_timeout
	// and the board silently stops receiving bids until it reconnects. The load
	// stream already heartbeats on the same interval.
	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-clientGone:
			return
		case <-ticker.C:
			c.SSEvent("ping", "")
			flusher.Flush()
		case msg, open := <-ch:
			if !open {
				return
			}
			if msg.CompanyID != companyID {
				continue
			}
			payload, err := json.Marshal(msg)
			if err != nil {
				continue
			}
			c.SSEvent("bid", json.RawMessage(payload))
			flusher.Flush()
		}
	}
}

func (h *Handler) GetDriverBids(c *gin.Context) {
	val, ok := c.Get(string(middleware.DriverContextKey))
	if !ok {
		c.JSON(http.StatusUnauthorized, utils.Response{Success: false, Error: "driver authentication required"})
		return
	}
	driverID := val.(int)

	bids, err := h.service.GetDriverBids(driverID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, utils.Response{Success: false, Error: "failed to fetch bids"})
		return
	}

	loadUUIDs := make([]string, 0, len(bids))
	seenLoads := map[string]bool{}
	for _, b := range bids {
		if !seenLoads[b.LoadID] {
			loadUUIDs = append(loadUUIDs, b.LoadID)
			seenLoads[b.LoadID] = true
		}
	}

	loads, err := h.service.GetLoadsByUUIDs(loadUUIDs)
	if err != nil {
		c.JSON(http.StatusInternalServerError, utils.Response{Success: false, Error: "failed to fetch load details"})
		return
	}

	loadMap := make(map[string]database.Load, len(loads))
	for _, l := range loads {
		loadMap[l.ID.String()] = l
	}

	result := make([]DriverBidResponse, 0, len(bids))
	for _, b := range bids {
		result = append(result, ToDriverBidResponse(b, loadMap))
	}

	c.JSON(http.StatusOK, utils.Response{
		Success: true,
		Data:    result,
	})
}

func (h *Handler) GetCompanyBids(c *gin.Context) {
    companyID := GetCompanyID(c)
    if companyID == 0 {
        c.JSON(http.StatusUnauthorized, utils.Response{Success: false, Error: "unauthorized"})
        return
    }

    pagination := ParseBidPagination(c)
    filter     := ParseBidListFilter(c)
    bids, total, err := h.service.GetCompanyBids(companyID, pagination, filter)
    if err != nil {
        c.JSON(http.StatusInternalServerError, utils.Response{Success: false, Error: "failed to fetch bids"})
        return
    }

    loadUUIDs    := make([]string, 0)
    var userIDs  []int64
    var driverIDs []int
    seenLoads   := map[string]bool{}
    seenUsers   := map[int64]bool{}
    seenDrivers := map[int]bool{}

    for _, b := range bids {
        if !seenLoads[b.LoadID] {
            loadUUIDs = append(loadUUIDs, b.LoadID)
            seenLoads[b.LoadID] = true
        }
        if b.UserID != nil && !seenUsers[*b.UserID] {
            userIDs = append(userIDs, *b.UserID)
            seenUsers[*b.UserID] = true
        }
        if b.DriverID != nil && !seenDrivers[*b.DriverID] {
            driverIDs = append(driverIDs, *b.DriverID)
            seenDrivers[*b.DriverID] = true
        }
    }

    loads, _      := h.service.GetLoadsByUUIDs(loadUUIDs)
    users, usersErr   := h.resolver.FetchUsers(userIDs)
    drivers, driversErr := h.resolver.FetchDrivers(driverIDs)
    driverBids, _ := h.service.GetDriverBidAmounts(loadUUIDs)
    bidPlaced, _  := h.service.LoadsWithDispatcherBid(loadUUIDs, companyID)

    // A failure here leaves the board showing bids with no name attached, so it
    // must be visible in the log rather than swallowed.
    if usersErr != nil {
        log.Printf("GetCompanyBids: could not resolve dispatchers: %v", usersErr)
    }
    if driversErr != nil {
        log.Printf("GetCompanyBids: could not resolve drivers: %v", driversErr)
    }

    loadMap := make(map[string]database.Load, len(loads))
    for _, l := range loads {
        loadMap[l.ID.String()] = l
    }

    result := make([]CompanyBidResponse, 0, len(bids))
    for _, b := range bids {
        item := ToCompanyBidResponse(b, users, drivers, driverBids, loadMap, bidPlaced)

        if !filter.MatchesDispatcher(item.Dispatcher) {
			if total > 0 { total-- }
			continue
		}
		if !filter.MatchesDriver(item.Driver) {
			if total > 0 { total-- }
			continue
		}

        result = append(result, item)
    }

    c.JSON(http.StatusOK, utils.Response{
        Success: true,
        Data:    result,
        Metadata: &utils.Metadata{
            CurrentPage:  int64(pagination.Page),
            PageSize:     int64(pagination.Limit),
            TotalRecords: total,
            LastPage:   (total + int64(pagination.Limit) - 1) / int64(pagination.Limit),
        },
    })
}
