package server

import (
	"net/http"

	"github.com/gin-gonic/gin"

	"github.com/makhammatovb/atrek/internal/utils"
)

func (app *Application) Routes() *gin.Engine {
	if app.Cfg.IsProduction() {
		gin.SetMode(gin.ReleaseMode)
	}
	router := gin.New()

	router.Use(app.Middleware.CORS())
	router.Use(gin.Logger())
	router.Use(app.Middleware.RecoveryMiddleware())

	public := router.Group("/api/v1")
	{
		public.GET("/health", healthCheck)
	}

	authenticated := router.Group("/api/v1")
	authenticated.Use(app.Middleware.Authenticate())
	{
		// loads
		// authenticated.GET("/loads/stream", app.LoadHandler.Stream)
		authenticated.GET("/loads", app.LoadHandler.List)
		// authenticated.GET("/loads/:id", app.LoadHandler.Retrieve)

		// bids
		authenticated.POST("/loads/:id/view", app.BidHandler.View)
		authenticated.GET("/loads/:id/bids", app.BidHandler.GetBids)
		authenticated.GET("/loads/bids/stream", app.BidHandler.Stream)
		authenticated.GET("/loads/driver/bids", app.BidHandler.GetDriverBids)
		authenticated.GET("/bids", app.BidHandler.GetCompanyBids)

		// bid board
		authenticated.POST("/bids/:bid_id/ignore", app.BidHandler.Ignore)
		authenticated.POST("/bids/:bid_id/unignore", app.BidHandler.Unignore)
	}

	botAccessible := router.Group("/api/v1")
    botAccessible.Use(app.Middleware.AuthenticateOrInternal())
	{
        botAccessible.GET("/loads/stream", app.LoadHandler.Stream)
        botAccessible.GET("/loads/:id", app.LoadHandler.Retrieve)
        // The telegram bot records driver bids and the email-agent records
        // dispatcher bids; neither carries a JWT.
        botAccessible.POST("/loads/:id/bid", app.BidHandler.Bid)
    }
	return router
}

func healthCheck(c *gin.Context) {
	c.JSON(http.StatusOK, utils.Response{
		Success: true,
		Message: "V1 available",
	})
}
