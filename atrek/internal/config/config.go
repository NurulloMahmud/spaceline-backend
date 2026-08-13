package config

import (
	"strconv"
	"strings"
)

type DBConfig struct {
	URL      string
	Host     string
	Name     string
	Port     string
	User     string
	Password string
}

type RedisConfig struct {
	Addr     string
	Password string
	DB       int
}

type S3Bucket struct {
	Region    string
	Name      string
	AccessKey string
	SecretKey string
	Prefix    string
}

type AtrekConfig struct {
	WSURL    string
	Username string
	Password string
	BaseURL  string
}

type Config struct {
	Env            string
	Port           string
	JWTSecret      string
	BaseURL        string
	AllowedOrigins []string
	DjangoBaseURL  string
	InternalSecret string
	DBConfig
	RedisConfig
	S3Bucket
	AtrekConfig
}

func Load() (*Config, error) {
	env := getEnv("ENV", "development")
	if env != "production" {
		loadEnvFile(".env")
	}

	db := DBConfig{
		URL:      getEnv("DATABASE_URL", "postgres://postgres:postgres@localhost:5432/atrek?sslmode=disable"),
		Host:     getEnv("DB_HOST", "localhost"),
		Name:     getEnv("DB_NAME", "ecommerce"),
		Port:     getEnv("DB_PORT", "5432"),
		User:     getEnv("DB_USER", "postgres"),
		Password: getEnv("DB_PASSWORD", ""),
	}

	rdb, _ := strconv.Atoi(getEnv("REDIS_DB", "0"))
	redis := RedisConfig{
		Addr:     getEnv("REDIS_ADDR", "localhost:6379"),
		Password: getEnv("REDIS_PASSWORD", ""),
		DB:       rdb,
	}

	bucket := S3Bucket{
		Name:      getEnv("BUCKET_NAME", ""),
		Region:    getEnv("BUCKET_REGION", ""),
		AccessKey: getEnv("BUCKET_ACCESS_KEY", ""),
		SecretKey: getEnv("BUCKET_SECRET_KEY", ""),
	}

	atrek := AtrekConfig{
		WSURL:    getEnv("ATREK_WS_URL", "ws://localhost:8080"),
		Username: getEnv("ATREK_USERNAME", ""),
		Password: getEnv("ATREK_PASSWORD", ""),
		BaseURL:  getEnv("ATREK_BASE_URL", "http://localhost:8080"),
	}

	rawOrigins := getEnv("ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:5173")

	origins := strings.Split(rawOrigins, ",")

	for i := range origins {
		origins[i] = strings.TrimSpace(origins[i])
	}

	cfg := Config{
		Env:            getEnv("ENV", "development"),
		Port:           getEnv("PORT", ":8000"),
		BaseURL:        getEnv("BASE_URL", "http://localhost"),
		JWTSecret:      getEnv("JWT_SECRET", "QBFHIKBZGOOQCBBFYHQNSOY"),
		AllowedOrigins: origins,
		DBConfig:       db,
		RedisConfig:    redis,
		S3Bucket:       bucket,
		AtrekConfig:    atrek,
		DjangoBaseURL:  getEnv("DJANGO_BASE_URL", "http://localhost"),
		InternalSecret: getEnv("INTERNAL_SECRET_KEY", ""),
	}

	return &cfg, nil
}
