package server

import (
	"log"
	"net/http"
	"time"

	"github.com/makhammatovb/atrek/internal/bids"
	"github.com/makhammatovb/atrek/internal/config"
	"github.com/makhammatovb/atrek/internal/loads"
	"github.com/makhammatovb/atrek/internal/middleware"
	"github.com/makhammatovb/atrek/pkg/atrek"
	"github.com/makhammatovb/atrek/pkg/storage"
	"github.com/redis/go-redis/v9"
	"gorm.io/gorm"
)

type Application struct {
	Cfg         config.Config
	DB          *gorm.DB
	RDB         *redis.Client
	Bucket      *storage.S3CLient
	LoadHandler *loads.Handler
	BidHandler  *bids.Handler
	Middleware  middleware.Middleware

	loadService     *loads.Service
	bidHub          *bids.Hub
	atrekWSClient   *atrek.Client
	atrekHTTPClient *atrek.HTTPClient
}

func NewApplication(cfg config.Config, db *gorm.DB, rdb *redis.Client, bucket *storage.S3CLient) (*Application, error) {
	atrekWSClient := atrek.NewClient(cfg.AtrekConfig.WSURL)

	atrekHTTPClient := atrek.NewHTTPClient(
		cfg.AtrekConfig.BaseURL,
		cfg.AtrekConfig.Username,
		cfg.AtrekConfig.Password,
	)

	bidHub := bids.NewHub()
	resolver := bids.NewResolver(
		cfg.DjangoBaseURL,
		&http.Client{Timeout: 5 * time.Second},
		cfg.InternalSecret,
	)
	bidRepo := bids.NewRepository(db)
	loadRepo := loads.NewRepository(db)

	bidService := bids.NewService(bidRepo, loadRepo, bidHub)
	loadService := loads.NewService(loadRepo, atrekHTTPClient, bidRepo)
	
	loadHandler := loads.NewHandler(loadService)
	bidHandler := bids.NewHandler(bidService, resolver)

	mw := middleware.Middleware{
		JWTSecret:      cfg.JWTSecret,
		RDB:            rdb,
		AllowedOrigins: cfg.AllowedOrigins,
		DjangoBaseURL:  cfg.DjangoBaseURL,
		InternalSecret: cfg.InternalSecret,
		HTTPClient: &http.Client{
			Timeout: 5 * time.Second,
		},
	}

	app := &Application{
		Cfg:             cfg,
		DB:              db,
		RDB:             rdb,
		Bucket:          bucket,
		LoadHandler:     loadHandler,
		bidHub:          bidHub,
		BidHandler:      bidHandler,
		Middleware:      mw,
		loadService:     loadService,
		atrekWSClient:   atrekWSClient,
		atrekHTTPClient: atrekHTTPClient,
	}
	return app, nil
}

func (app *Application) StartBackgroundWorkers() {
	if app.Cfg.AtrekConfig.WSURL == "" {
		log.Println("WARNING: ATREK_WS_URL is not set - skipping Atrek connection, no loads will be received")
		return
	}

	go app.atrekWSClient.Run()
	go app.loadService.ConsumeAtrekEvents(app.atrekWSClient.Events())
	log.Printf("atrek: connecting to %s", app.Cfg.AtrekConfig.WSURL)
}
