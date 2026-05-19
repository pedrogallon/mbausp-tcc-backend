package br.com.pedrogallonalves.tccmba.backend.config;


import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;

@Slf4j
@Component
@ConditionalOnProperty(name = "app.mode", havingValue = "request", matchIfMissing = false)
public class AwsRequestHealthCheck {

    private final DynamoDbClient dynamoDbClient;

    public AwsRequestHealthCheck(DynamoDbClient dynamoDbClient) {
        this.dynamoDbClient = dynamoDbClient;
    }

    @EventListener(ApplicationReadyEvent.class)
    public void checkAwsConnection() {
        try {
            if (dynamoDbClient != null) {
                log.info("✅ AWS DynamoDB client initialized");
            } else {
                log.warn("⚠️  DynamoDB client is null (expected in request mode)");
            }
            log.info("🔗 AWS credentials loaded successfully");
        } catch (Exception e) {
            log.error("❌ AWS connection failed: {}", e.getMessage(), e);
        }
    }
}
