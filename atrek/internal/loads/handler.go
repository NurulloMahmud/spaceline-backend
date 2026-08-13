package loads

import (
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/makhammatovb/atrek/internal/middleware"
	"github.com/makhammatovb/atrek/internal/utils"
	auth "github.com/makhammatovb/atrek/pkg/jwt"
)

func SSEHeaders() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Header("Content-Type", "text/event-stream")
		c.Header("Cache-Control", "no-cache")
		c.Header("Connection", "keep-alive")
		c.Header("X-Accel-Buffering", "no")
		c.Next()
	}
}

type Handler struct {
	service *Service
}

func NewHandler(service *Service) *Handler {
	return &Handler{service: service}
}

func (h *Handler) Stream(c *gin.Context) {
    flusher, ok := c.Writer.(http.Flusher)
    if !ok {
        c.JSON(http.StatusInternalServerError, gin.H{"error": "streaming unsupported"})
        return
    }

    isInternal := false
    if val, exists := c.Get("is_internal"); exists {
        isInternal = val.(bool)
    }

    driverFilter := ParseDriverFilter(c)
    loadFilter   := ParseLoadFilter(c)

    ch := h.service.Subscribe()
    defer h.service.Unsubscribe(ch)

    c.Header("Content-Type", "text/event-stream")
    c.Header("Cache-Control", "no-cache")
    c.Header("Connection", "keep-alive")
    c.Header("X-Accel-Buffering", "no")

    c.SSEvent("", "connected")
    flusher.Flush()

    clientGone := c.Request.Context().Done()
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
            if !isInternal {
                if driverFilter.Active {
                    if msg.PickUpLat == 0 && msg.PickUpLng == 0 {
                        continue
                    }
                    if !driverFilter.Matches(msg.PickUpLat, msg.PickUpLng) {
                        continue
                    }
                }
                if !loadFilter.Matches(msg) {
                    continue
                }
            }
            payload, err := json.Marshal(msg)
            if err != nil {
                continue
            }
            c.SSEvent("load", json.RawMessage(payload))
            flusher.Flush()
        }
    }
}

func (h *Handler) List(c *gin.Context) {
	var filter utils.Filter
	if err := c.ShouldBindQuery(&filter); err != nil {
		c.JSON(http.StatusBadRequest, utils.Response{
			Success: false,
			Error:   err.Error(),
		})
		return
	}

	if val, ok := c.Get(string(middleware.UserContextKey)); ok {
		claims := val.(*auth.TokenClaims)
		filter.CompanyID = int(claims.CompanyID)
	}

	if filter.Page == 0 {
		filter.Page = 1
	}
	if filter.Limit == 0 {
		filter.Limit = 20
	}
	driverFilter := ParseDriverFilter(c)
	loadFilter := ParseLoadFilter(c)
	resp, err := h.service.ListLoads(filter, driverFilter, loadFilter)
	if err != nil {
		c.JSON(http.StatusInternalServerError, utils.Response{
			Success: false,
			Error:   "failed to fetch loads",
		})
		return
	}

	c.JSON(http.StatusOK, utils.Response{
		Success:  true,
		Data:     resp.Items,
		Metadata: &resp.Metadata,
	})
}

func (h *Handler) Retrieve(c *gin.Context) {
	id := c.Param("id")
	if id == "" {
		c.JSON(http.StatusBadRequest, utils.Response{
			Success: false,
			Error:   utils.ErrInvalidID.Error(),
		})
		return
	}

	data, err := h.service.RetrieveLoad(id)
	if err != nil {
		status := http.StatusInternalServerError
		switch {
		case errors.Is(err, utils.ErrNotFound):
			status = http.StatusNotFound
		case errors.Is(err, utils.ErrAtrekFetch):
			status = http.StatusBadGateway
		}
		c.JSON(status, utils.Response{
			Success: false,
			Error:   err.Error(),
		})
		return
	}

	c.JSON(http.StatusOK, utils.Response{
		Success: true,
		Data:    data,
	})
}
