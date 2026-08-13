package main

import (
	"flag"
	"net/http"
	"time"

	"github.com/makhammatovb/atrek/internal/config"
	"github.com/makhammatovb/atrek/internal/server"
	"github.com/makhammatovb/atrek/pkg/database"
	"github.com/makhammatovb/atrek/pkg/logger"
	"github.com/makhammatovb/atrek/pkg/redis"
	"github.com/makhammatovb/atrek/pkg/storage"
)

func main() {
	logger.Init(true)
	logger.Log.Info().Msg("Starting application....")

	migrate := flag.Bool("migrate", false, "run database migrations")
	flag.Parse()

	cfg, err := config.Load()
	if err != nil {
		panic(err)
	}

	pgDB, err := database.Connect(*cfg)
	if err != nil {
		logger.Log.Error().
			Err(err).
			Msg("Failed to connect to database")
		panic(err)
	}

	logger.Log.Info().Msg("Connected to database successfully")

	if *migrate {
		err = database.Migrate(pgDB)
		if err != nil {
			logger.Log.Error().
				Err(err).
				Msg("failed to migrate database in postgres")
			panic(err)
		}
		return
	}

	rdb, err := redis.NewClient(*cfg)
	if err != nil {
		panic(err)
	}

	bucket, err := storage.NewS3Client(*cfg)
	if err != nil {
		if cfg.IsProduction() {
			logger.Log.Error().
				Err(err).
				Msg("Failed to connect to S3 bucket in production")

			panic(err)
		}

		logger.Log.Warn().
			Err(err).
			Msg("Failed to connect Storage in development, it's ok for now")
	}

	app, err := server.NewApplication(*cfg, pgDB, rdb, bucket)
	if err != nil {
		panic(err)
	}
	app.StartBackgroundWorkers()
	srv := &http.Server{
		Addr:         cfg.Port,
		Handler:      app.Routes(),
		IdleTimeout:  time.Minute,
		ReadTimeout:  10 * time.Second,
		WriteTimeout: 30 * time.Second,
	}

	logger.Log.Printf("starting server on %s", cfg.Port)

	err = srv.ListenAndServe()
	if err != nil {
		logger.Log.Warn().Err(err)
	}
}
