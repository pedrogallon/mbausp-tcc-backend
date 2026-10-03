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

EXPOSE 8080
ENTRYPOINT ["java","-jar","/app/app.jar"]