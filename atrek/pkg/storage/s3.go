package storage

import (
	"context"
	"fmt"
	"mime/multipart"
	"path/filepath"
	"strings"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/google/uuid"
	cfg "github.com/makhammatovb/atrek/internal/config"
)

type S3CLient struct {
	client *s3.Client
	bucket string
	region string
	prefix string
}

func NewS3Client(c cfg.Config) (*S3CLient, error) {
	cfg, err := config.LoadDefaultConfig(context.Background(),
		config.WithRegion(c.S3Bucket.Region),
		config.WithCredentialsProvider(credentials.NewStaticCredentialsProvider(c.S3Bucket.AccessKey, c.S3Bucket.SecretKey, "")),
	)
	if err != nil {
		return nil, err
	}

	return &S3CLient{
		client: s3.NewFromConfig(cfg),
		bucket: c.S3Bucket.Name,
		region: c.S3Bucket.Region,
		prefix: c.S3Bucket.Prefix,
	}, nil
}

func (s *S3CLient) Upload(ctx context.Context, file multipart.File, header *multipart.FileHeader) (string, error) {
	ext := filepath.Ext(header.Filename)
	key := fmt.Sprintf("%s/%s/%s%s", s.prefix, time.Now().Format("2006/01/02"), uuid.New().String(), ext)

	_, err := s.client.PutObject(ctx, &s3.PutObjectInput{
		Bucket:        aws.String(s.bucket),
		Key:           aws.String(key),
		Body:          file,
		ContentLength: aws.Int64(header.Size),
	})
	if err != nil {
		return "", err
	}
	url := fmt.Sprintf("https://%s.s3.%s.amazonaws.com/%s", s.bucket, s.region, key)
	return url, nil
}

func (s *S3CLient) Delete(ctx context.Context, key string) error {
	_, err := s.client.DeleteObject(ctx, &s3.DeleteObjectInput{
		Bucket: aws.String(s.bucket),
		Key:    aws.String(key),
	})
	return err
}

func ExtractKeyFromURL(url string) string {
	parts := strings.SplitN(url, ".amazonaws.com/", 2)
	if len(parts) != 2 {
		return ""
	}
	return parts[1]
}
