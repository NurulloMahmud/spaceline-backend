package bids

import (
    "strings"
    "strconv"

    "github.com/gin-gonic/gin"
)

type BidListFilter struct {
    PickUpState  string
    DeliverState string
    Dispatcher   string
    DriverName   string
    Brokerage    string
    Action       string
    Ignored      *bool
    // BidPlaced filters on whether the load already carries a dispatcher bid
    // for this company. Driver bids on such a load are off the board by
    // default: the load is in negotiation and cannot be bid again.
    BidPlaced *bool
}

func ParseBidListFilter(c *gin.Context) BidListFilter {
    f := BidListFilter{
        PickUpState:  strings.TrimSpace(c.Query("pick_up_state")),
        DeliverState: strings.TrimSpace(c.Query("deliver_state")),
        Dispatcher:   strings.TrimSpace(c.Query("dispatcher")),
        DriverName:   strings.TrimSpace(c.Query("driver")),
        Brokerage:    strings.TrimSpace(c.Query("brokerage")),
        Action:       strings.TrimSpace(c.Query("action")),
    }

    // Existing dispatcher-bid consumers call this endpoint without an action.
    if f.Action != "driver_bid" && f.Action != "viewed" {
        f.Action = "dispatcher_bid"
    }

    if v := strings.TrimSpace(c.Query("ignored")); v != "" {
        ignored := v == "true" || v == "1"
        f.Ignored = &ignored
    }

    if v := strings.TrimSpace(c.Query("bid_placed")); v != "" {
        placed := v == "true" || v == "1"
        f.BidPlaced = &placed
    } else if f.Action == "driver_bid" {
        // Once dispatch has bid a load to the broker it lives in negotiations,
        // and a second bid on it is refused, so its driver bids leave the
        // board. Pass bid_placed=true to review them.
        placed := false
        f.BidPlaced = &placed
    }

    return f
}

type BidPagination struct {
    Page  int
    Limit int
}

func ParseBidPagination(c *gin.Context) BidPagination {
    p := BidPagination{Page: 1, Limit: 20}
    if v := c.Query("page"); v != "" {
        if n, err := strconv.Atoi(v); err == nil && n > 0 {
            p.Page = n
        }
    }
    if v := c.Query("limit"); v != "" {
        if n, err := strconv.Atoi(v); err == nil && n > 0 {
            p.Limit = n
        }
    }
    return p
}

func (f *BidListFilter) MatchesDispatcher(u *UserInfo) bool {
    if f.Dispatcher == "" {
        return true
    }
    if u == nil {
        return false
    }
    fullName := strings.ToLower(u.FirstName + " " + u.LastName)
    return strings.Contains(fullName, strings.ToLower(f.Dispatcher))
}

func (f *BidListFilter) MatchesDriver(d *DriverInfo) bool {
    if f.DriverName == "" {
        return true
    }
    if d == nil {
        return false
    }
    return strings.Contains(strings.ToLower(d.FullName), strings.ToLower(f.DriverName))
}
