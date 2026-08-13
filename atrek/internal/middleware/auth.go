package middleware

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"log"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"

	"github.com/makhammatovb/atrek/internal/utils"
	auth "github.com/makhammatovb/atrek/pkg/jwt"
)

type contextKey string

const (
	UserContextKey    contextKey = "user"
	DriverContextKey  contextKey = "driver"
	CompanyContextKey contextKey = "company_id"
)

type Middleware struct {
	JWTSecret      string
	RDB            *redis.Client
	AllowedOrigins []string
	DjangoBaseURL  string
	HTTPClient     *http.Client
	InternalSecret string
}

func UserFromContext(ctx context.Context) *auth.TokenClaims {
	user, ok := ctx.Value(UserContextKey).(*auth.TokenClaims)
	if !ok {
		return nil
	}
	return user
}

func DriverFromContext(ctx context.Context) (int, bool) {
	driverID, ok := ctx.Value(DriverContextKey).(int)
	return driverID, ok
}

func (m *Middleware) CORS() gin.HandlerFunc {
	allowed := make(map[string]bool, len(m.AllowedOrigins))
	for _, o := range m.AllowedOrigins {
		allowed[o] = true
	}

	return func(c *gin.Context) {
		origin := c.GetHeader("Origin")
		c.Header("Vary", "Origin")

		if origin != "" && allowed[origin] {
			c.Header("Access-Control-Allow-Origin", origin)
			c.Header("Access-Control-Allow-Credentials", "true")
			c.Header("Access-Control-Allow-Methods", "GET, POST, PATCH, PUT, DELETE, OPTIONS")
			c.Header("Access-Control-Allow-Headers", "Authorization, Driver-Authorization, Content-Type")
		}

		if c.Request.Method == http.MethodOptions {
			c.AbortWithStatus(http.StatusNoContent)
			return
		}

		c.Next()
	}
}

func (m *Middleware) Authenticate() gin.HandlerFunc {
	return func(c *gin.Context) {
		c.Header("Vary", "Authorization")
		authHeader := c.GetHeader("Authorization")
		if authHeader != "" {
			headerParts := strings.SplitN(authHeader, " ", 2)
			if len(headerParts) != 2 || !strings.EqualFold(headerParts[0], "Bearer") {
				writeError(c, http.StatusUnauthorized, "authorization header must be 'Bearer <token>'")
				return
			}

			claims, err := auth.VerifyToken(headerParts[1], m.JWTSecret)
			if err != nil {
				log.Printf("auth: token verification failed: %v", err)
				msg := "invalid or expired token"
				if errors.Is(err, auth.ErrNotAccessToken) {
					msg = "refresh tokens cannot be used to authenticate requests"
				}
				writeError(c, http.StatusUnauthorized, msg)
				return
			}

			c.Set(string(UserContextKey), claims)
			ctx := context.WithValue(c.Request.Context(), UserContextKey, claims)
			c.Request = c.Request.WithContext(ctx)
			c.Next()
			return
		}

		// The browser's EventSource cannot set request headers, so streaming
		// endpoints take the access token as a query parameter instead. Limited
		// to /stream paths so ordinary requests never put a token in the access
		// log. Documented in FRONTEND_API_GUIDE.md as `?token=`.
		if queryToken := c.Query("token"); queryToken != "" &&
			strings.HasSuffix(c.Request.URL.Path, "/stream") {

			claims, err := auth.VerifyToken(queryToken, m.JWTSecret)
			if err != nil {
				log.Printf("auth: stream token verification failed: %v", err)
				msg := "invalid or expired token"
				if errors.Is(err, auth.ErrNotAccessToken) {
					msg = "refresh tokens cannot be used to authenticate requests"
				}
				writeError(c, http.StatusUnauthorized, msg)
				return
			}

			c.Set(string(UserContextKey), claims)
			ctx := context.WithValue(c.Request.Context(), UserContextKey, claims)
			c.Request = c.Request.WithContext(ctx)
			c.Next()
			return
		}

		driverToken := c.GetHeader("Driver-Authorization")
		if driverToken != "" {
			driverID, companyID, err := m.validateDriverToken(c.Request.Context(), driverToken)
			if err != nil {
				log.Printf("auth: driver token validation failed: %v", err)
				writeError(c, http.StatusUnauthorized, "invalid or expired driver token")
				return
			}
			c.Set(string(DriverContextKey), driverID)
			c.Set(string(CompanyContextKey), companyID)
			ctx := context.WithValue(c.Request.Context(), DriverContextKey, driverID)
			ctx = context.WithValue(ctx, CompanyContextKey, companyID)
			c.Request = c.Request.WithContext(ctx)
			c.Next()
			return
		}

		writeError(c, http.StatusUnauthorized, "missing authorization header")
	}
}

func (m *Middleware) RecoveryMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		defer func() {
			if rec := recover(); rec != nil {
				log.Printf("panic recovered: %v", rec)
				writeError(c, http.StatusInternalServerError, "internal server error")
				c.Abort()
			}
		}()
		c.Next()
	}
}

func writeError(c *gin.Context, status int, message string) {
	c.AbortWithStatusJSON(status, utils.Response{
		Success: false,
		Error:   message,
	})
}

func (m *Middleware) validateDriverToken(ctx context.Context, token string) (int, int, error) {
	body, err := json.Marshal(map[string]string{"token": token})
	if err != nil {
		return 0, 0, err
	}

	req, err := http.NewRequestWithContext(
		ctx,
		http.MethodPost,
		m.DjangoBaseURL+"mobile/auth/verify-driver-token/",
		bytes.NewBuffer(body),
	)
	if err != nil {
		return 0, 0, err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := m.HTTPClient.Do(req)
	if err != nil {
		return 0, 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, 0, errors.New("invalid or expired driver token")
	}

	var result struct {
		DriverID  int `json:"driver_id"`
		CompanyID int `json:"company_id"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return 0, 0, err
	}

	if result.DriverID == 0 {
		return 0, 0, errors.New("invalid driver token response")
	}

	return result.DriverID, result.CompanyID, nil
}

func (m *Middleware) AuthenticateOrInternal() gin.HandlerFunc {
	return func(c *gin.Context) {
		secret := c.GetHeader("X-Internal-Secret")
		if secret != "" {
			if secret == m.InternalSecret {
				c.Set("is_internal", true)
				c.Next()
				return
			}
			writeError(c, http.StatusUnauthorized, "invalid internal secret")
			return
		}

		m.Authenticate()(c)
	}
}
