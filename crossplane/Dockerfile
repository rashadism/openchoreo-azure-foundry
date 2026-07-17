# Multi-stage build for the FoundryAgent Crossplane provider.
FROM golang:1.24 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -o /provider ./cmd/provider

FROM gcr.io/distroless/static:nonroot
COPY --from=build /provider /provider
USER nonroot:nonroot
ENTRYPOINT ["/provider"]
