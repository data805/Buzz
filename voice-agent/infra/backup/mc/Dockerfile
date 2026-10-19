# MinIO client (mc) built from source at its final community release.
#
# MinIO archived mc and removed its images (Docker Hub) and binaries
# (dl.min.io returns 410 Gone), and dograhai/minio ships no mc. So we build the
# last tag ourselves. Build once on the app host:
#   docker build -t buzz/mc:RELEASE.2025-08-13T08-35-41Z infra/backup/mc
# The build needs GitHub and the Go module proxy; do it in a maintenance window
# (or build elsewhere and `docker save | docker load`), since egress is deny-by-default.
FROM golang:1.25.3-alpine3.22 AS build
ARG MC_TAG=RELEASE.2025-08-13T08-35-41Z
RUN apk add --no-cache git
RUN git clone --depth 1 --branch "${MC_TAG}" https://github.com/minio/mc.git /src
WORKDIR /src
RUN CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o /out/mc .

FROM alpine:3.22.2
RUN apk add --no-cache ca-certificates
COPY --from=build /out/mc /usr/bin/mc
COPY --from=build /src/LICENSE /licenses/mc-LICENSE
ENV HOME=/tmp
ENTRYPOINT ["/usr/bin/mc"]
