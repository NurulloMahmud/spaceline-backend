package redis

import (
	"context"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/makhammatovb/atrek/internal/config"
	"github.com/redis/go-redis/v9"
)

func NewClient(cfg config.Config) (*redis.Client, error) {
	rdb := redis.NewClient(&redis.Options{
		Addr:     cfg.RedisConfig.Addr,
		Password: cfg.RedisConfig.Password,
		DB:       cfg.RedisConfig.DB,
	})

	if err := rdb.Ping(context.Background()).Err(); err != nil {
		return nil, err
	}

	return rdb, nil
}

func RecordFailedAttempt(ctx context.Context, rdb *redis.Client, userID uuid.UUID) error {
	key := fmt.Sprintf("login_attempts:%s", userID)
	now := float64(time.Now().Unix()) // need to be figure out
	uniqueID := fmt.Sprintf("%d-%s", time.Now().UnixNano(), uuid.New().String())

	err := rdb.ZAdd(ctx, key, redis.Z{Score: now, Member: uniqueID}).Err()
	if err != nil {
		return err
	}

	if err := rdb.Expire(ctx, key, 24*time.Hour).Err(); err != nil {
		return err
	}
	return nil
}

func IsBlocked(ctx context.Context, rdb *redis.Client, userID uuid.UUID) (bool, error) {
	key := fmt.Sprintf("login_attempts:%s", userID)
	cutoff := float64(time.Now().Add(-24 * time.Hour).Unix())

	err := rdb.ZRemRangeByScore(ctx, key, "-inf", fmt.Sprintf("%f", cutoff)).Err()
	if err != nil {
		return false, err
	}

	count, err := rdb.ZCard(ctx, key).Result()
	if err != nil {
		return false, err
	}

	return count >= 5, nil
}
