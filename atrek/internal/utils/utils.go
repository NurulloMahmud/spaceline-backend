package utils

import (
	"errors"
	"regexp"
)

var (
	ErrEmailFormat = errors.New("Invalid email")
	ErrNotFound    = errors.New("record not found")
	ErrAtrekFetch  = errors.New("failed to fetch from atrek")
	ErrInvalidID   = errors.New("invalid id")
)

type Filter struct {
	Page        int     `form:"page" binding:"omitempty,min=1"`
	Limit       int     `form:"limit" binding:"omitempty,min=1,max=2147483647"` // max value is 32-bit  integer
	Sort        string  `form:"sort"`
	Search      string  `form:"search"`
	CompanyID   int     `form:"company_id"`
	IsActive    *bool   `form:"is_active"`
}

type Metadata struct {
	CurrentPage  int64 `json:"current_page,omitzero"`
	PageSize     int64 `json:"page_size,omitzero"`
	FirstPage    int64 `json:"first_page,omitzero"`
	LastPage     int64 `json:"last_page,omitzero"`
	TotalRecords int64 `json:"total_records,omitzero"`
}

func CalculateMetadata(totalRecords, page, pageSize int64) Metadata {
	if totalRecords == 0 {
		return Metadata{}
	}

	return Metadata{
		CurrentPage:  page,
		PageSize:     pageSize,
		FirstPage:    1,
		LastPage:     (totalRecords + pageSize - 1) / pageSize,
		TotalRecords: totalRecords,
	}
}

type Response struct {
	Success  bool        `json:"success"`
	Metadata *Metadata   `json:"meta_data,omitempty"`
	Error    string      `json:"error,omitempty"`
	Data     interface{} `json:"data,omitempty"`
	Message  string      `json:"message,omitempty"`
}

func ValidateEmailFormat(email string) error {
	emailRegex := regexp.MustCompile(`^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$`)
	if !emailRegex.MatchString(email) {
		return ErrEmailFormat
	}
	return nil
}
