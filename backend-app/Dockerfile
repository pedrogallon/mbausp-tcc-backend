FROM eclipse-temurin:25-jdk-jammy AS builder
WORKDIR /app
COPY . .
RUN ./gradlew clean build -x test

FROM eclipse-temurin:25-jre-jammy
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/build/libs/ /app/libs/
RUN find /app/libs -maxdepth 1 -type f -name '*.jar' ! -name '*-plain.jar' -exec cp {} /app/app.jar \;

# Local EMF sink writes JSON to stdout; awslogs driver extracts metrics into CloudWatch.
ENV AWS_EMF_ENVIRONMENT=Local
ENV AWS_EMF_SERVICE_NAME=backend

EXPOSE 8080
ENTRYPOINT ["java","-jar","/app/app.jar"]
