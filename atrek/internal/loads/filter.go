package loads

import (
	"strconv"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/makhammatovb/atrek/pkg/haversine"
)

type DriverFilter struct {
	DriverIDs  []string
	DriverLats []float64
	DriverLngs []float64
	Radius     float64
	Active     bool
}

type LoadFilter struct {
	PickUpState  string
	PickUpCity   string
	DeliverState string
	DeliverCity  string
	VehicleType  string
	MilesMin     int
	MilesMax     int
	Brokerage    string
}

func ParseDriverFilter(c *gin.Context) DriverFilter {
	f := DriverFilter{}

	idsStr := c.Query("driver_ids")
	latsStr := c.Query("driver_lats")
	lngsStr := c.Query("driver_lngs")
	radiusStr := c.Query("radius")

	if idsStr == "" || latsStr == "" || lngsStr == "" {
		return f
	}

	ids := strings.Split(idsStr, ",")
	lats := strings.Split(latsStr, ",")
	lngs := strings.Split(lngsStr, ",")

	if len(ids) != len(lats) || len(ids) != len(lngs) {
		return f
	}

	parsedLats := make([]float64, 0, len(lats))
	parsedLngs := make([]float64, 0, len(lngs))

	for i := range lats {
		lat, err1 := strconv.ParseFloat(strings.TrimSpace(lats[i]), 64)
		lng, err2 := strconv.ParseFloat(strings.TrimSpace(lngs[i]), 64)
		if err1 != nil || err2 != nil {
			return f
		}
		parsedLats = append(parsedLats, lat)
		parsedLngs = append(parsedLngs, lng)
	}

	radius := 100.0
	if radiusStr != "" {
		if r, err := strconv.ParseFloat(radiusStr, 64); err == nil {
			radius = r
		}
	}

	f.DriverIDs = ids
	f.DriverLats = parsedLats
	f.DriverLngs = parsedLngs
	f.Radius = radius
	f.Active = true
	return f
}

func ParseLoadFilter(c *gin.Context) LoadFilter {
	f := LoadFilter{}

	f.PickUpState = strings.TrimSpace(c.Query("pick_up_state"))
	f.PickUpCity = strings.ToLower(strings.TrimSpace(c.Query("pick_up_city")))
	f.DeliverState = strings.TrimSpace(c.Query("deliver_state"))
	f.DeliverCity = strings.ToLower(strings.TrimSpace(c.Query("deliver_city")))
	f.VehicleType = strings.ToLower(strings.TrimSpace(c.Query("vehicle_type")))
	f.Brokerage = strings.ToLower(strings.TrimSpace(c.Query("brokerage")))
	if min := c.Query("miles_min"); min != "" {
		if v, err := strconv.Atoi(min); err == nil {
			f.MilesMin = v
		}
	}
	if max := c.Query("miles_max"); max != "" {
		if v, err := strconv.Atoi(max); err == nil {
			f.MilesMax = v
		}
	}
	return f
}

func (f *DriverFilter) Matches(pickLat, pickLng float64) bool {
	if !f.Active {
		return true
	}
	return haversine.AnyDriverWithinRadius(pickLat, pickLng, f.DriverLats, f.DriverLngs, f.Radius)
}

func (f *LoadFilter) Matches(msg StreamMessage) bool {
	if f.PickUpState != "" && !strings.EqualFold(msg.PickUpAtState, f.PickUpState) {
		return false
	}
	if f.PickUpCity != "" && !strings.Contains(strings.ToLower(msg.PickUpAt), f.PickUpCity) {
		return false
	}
	if f.DeliverState != "" && !strings.EqualFold(msg.DeliverToState, f.DeliverState) {
		return false
	}
	if f.DeliverCity != "" && !strings.Contains(strings.ToLower(msg.DeliverTo), f.DeliverCity) {
		return false
	}
	if f.VehicleType != "" && !strings.Contains(strings.ToLower(msg.VehicleType), f.VehicleType) {
		return false
	}
	if f.Brokerage != "" && !strings.Contains(strings.ToLower(msg.ContactName), f.Brokerage) {
		return false
	}
	if f.MilesMin > 0 && msg.Miles < f.MilesMin {
		return false
	}
	if f.MilesMax > 0 && msg.Miles > f.MilesMax {
		return false
	}
	return true
}
