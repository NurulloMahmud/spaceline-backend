package logger


import (
	"os"
	"time"

	"github.com/rs/zerolog"
)

var Log zerolog.Logger

func Init(isDebug bool) {
	level := zerolog.InfoLevel

	if isDebug {
		level = zerolog.DebugLevel
	}

	Log = zerolog.New(os.Stdout). 
	Level(level). 
	With(). 
	Timestamp(). 
	Caller(). 
	Logger()

	if isDebug {
		Log = Log.Output(zerolog.ConsoleWriter{
			Out: os.Stdout,
			TimeFormat: time.RFC822,
		})
	}
}