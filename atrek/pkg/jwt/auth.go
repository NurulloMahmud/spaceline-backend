package jwt

import (
	"errors"
	"fmt"

	"github.com/golang-jwt/jwt/v5"
)

type TokenClaims struct {
	ID         int64  `json:"user_id"`
	Username   string `json:"username"`
	FirstName  string `json:"first_name"`
	LastName   string `json:"last_name"`
	Department string `json:"department"`
	CompanyID  int64  `json:"company_id"`
	Company    string `json:"company"`
	TokenType string `json:"token_type"`
	jwt.RegisteredClaims
}

var (
	ErrNotAccessToken = errors.New("token is not an access token")
)

func VerifyToken(tokenString, jwtSecret string) (*TokenClaims, error) {
	claims := &TokenClaims{}

	token, err := jwt.ParseWithClaims(tokenString, claims, func(t *jwt.Token) (interface{}, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("unexpected signing method: %v", t.Header["alg"])
		}
		return []byte(jwtSecret), nil
	})
	if err != nil {
		return nil, err
	}
	if !token.Valid {
		return nil, fmt.Errorf("invalid token")
	}

	if claims.TokenType != "access" {
		return nil, ErrNotAccessToken
	}

	return claims, nil
}
