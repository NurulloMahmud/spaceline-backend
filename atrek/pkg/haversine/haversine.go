package haversine

import "math"

func Distance(lat1, lng1, lat2, lng2 float64) float64 {
	const R = 3958.8
	lat1r := math.Pi * lat1 / 180
	lat2r := math.Pi * lat2 / 180
	dlat := math.Pi * (lat2 - lat1) / 180
	dlng := math.Pi * (lng2 - lng1) / 180

	a := math.Sin(dlat/2)*math.Sin(dlat/2) +
		math.Cos(lat1r)*math.Cos(lat2r)*
			math.Sin(dlng/2)*math.Sin(dlng/2)
	c := 2 * math.Atan2(math.Sqrt(a), math.Sqrt(1-a))

	return R * c
}

func AnyDriverWithinRadius(pickLat, pickLng float64, driverLats, driverLngs []float64, radius float64) bool {
	for i := range driverLats {
		if Distance(pickLat, pickLng, driverLats[i], driverLngs[i]) <= radius {
			return true
		}
	}
	return false
}
