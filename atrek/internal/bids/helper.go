package bids

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
	"github.com/makhammatovb/atrek/internal/middleware"
	auth "github.com/makhammatovb/atrek/pkg/jwt"
)

type UserInfo struct {
    ID        int64  `json:"id"`
    Username  string `json:"username"`
    FirstName string `json:"first_name"`
    LastName  string `json:"last_name"`
}

type DriverInfo struct {
    ID       int    `json:"id"`
    FullName string `json:"full_name"`
}

type Resolver struct {
    djangoBaseURL  string
    httpClient     *http.Client
    internalSecret string
}

func NewResolver(djangoBaseURL string, httpClient *http.Client, internalSecret string) *Resolver {
    return &Resolver{
        djangoBaseURL:  djangoBaseURL,
        httpClient:     httpClient,
        internalSecret: internalSecret,
    }
}

// apiURL builds a Django API path. DJANGO_BASE_URL is deployed with an "/api/"
// suffix (the driver-token middleware appends bare paths to it), so the suffix
// is normalised here rather than assumed — appending "/api/..." to it produced
// "/api/api/..." and every lookup 404'd.
func (r *Resolver) apiURL(path string) string {
    base := strings.TrimRight(r.djangoBaseURL, "/")
    if !strings.HasSuffix(base, "/api") {
        base += "/api"
    }
    return base + path
}

func (r *Resolver) FetchUsers(ids []int64) (map[int64]UserInfo, error) {
    if len(ids) == 0 {
        return map[int64]UserInfo{}, nil
    }

    strIDs := make([]string, len(ids))
    for i, id := range ids {
        strIDs[i] = fmt.Sprintf("%d", id)
    }

    url := r.apiURL("/users/users-bulk/?ids=" + strings.Join(strIDs, ","))
    req, err := http.NewRequest(http.MethodGet, url, nil)
    if err != nil {
        return nil, err
    }
    req.Header.Set("X-Internal-Secret", r.internalSecret)

    resp, err := r.httpClient.Do(req)
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()

    if resp.StatusCode != http.StatusOK {
        return nil, fmt.Errorf("users-bulk returned %d for %s", resp.StatusCode, url)
    }

    var users []UserInfo
    if err := json.NewDecoder(resp.Body).Decode(&users); err != nil {
        return nil, err
    }

    result := make(map[int64]UserInfo, len(users))
    for _, u := range users {
        result[u.ID] = u
    }
    return result, nil
}

func (r *Resolver) FetchDrivers(ids []int) (map[int]DriverInfo, error) {
    if len(ids) == 0 {
        return map[int]DriverInfo{}, nil
    }

    strIDs := make([]string, len(ids))
    for i, id := range ids {
        strIDs[i] = fmt.Sprintf("%d", id)
    }

    url := r.apiURL("/hiring/drivers-bulk/?ids=" + strings.Join(strIDs, ","))
    req, err := http.NewRequest(http.MethodGet, url, nil)
    if err != nil {
        return nil, err
    }
    req.Header.Set("X-Internal-Secret", r.internalSecret)

    resp, err := r.httpClient.Do(req)
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()

    if resp.StatusCode != http.StatusOK {
        return nil, fmt.Errorf("drivers-bulk returned %d for %s", resp.StatusCode, url)
    }

    var drivers []DriverInfo
    if err := json.NewDecoder(resp.Body).Decode(&drivers); err != nil {
        return nil, err
    }

    result := make(map[int]DriverInfo, len(drivers))
    for _, d := range drivers {
        result[d.ID] = d
    }
    return result, nil
}

func GetCompanyID(c *gin.Context) int {
    if val, ok := c.Get(string(middleware.UserContextKey)); ok {
        claims := val.(*auth.TokenClaims)
        return int(claims.CompanyID)
    }
    if val, ok := c.Get(string(middleware.CompanyContextKey)); ok {
        return val.(int)
    }
    return 0
}
