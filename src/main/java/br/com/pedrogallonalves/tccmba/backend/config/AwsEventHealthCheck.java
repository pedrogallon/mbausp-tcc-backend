package br.com.pedrogallonalves.tccmba.backend.config;


import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;
import software.amazon.awssdk.services.sqs.SqsClient;

@Slf4j
@Component
@ConditionalOnProperty(name = "app.mode", havingValue = "event", matchIfMissing = false)
public class AwsEventHealthCheck {

    private final DynamoDbClient dynamoDbClient;
    private final SqsClient sqsClient;

    public AwsEventHealthCheck(DynamoDbClient dynamoDbClient, SqsClient sqsClient) {
        this.dynamoDbClient = dynamoDbClient;
        this.sqsClient = sqsClient;
    }

    @EventListener(ApplicationReadyEvent.class)
    public void checkAwsConnection() {
        try {
            if (sqsClient != null) {
                log.info("✅ AWS SQS client initialized");
            } else {
                log.warn("⚠️  SQS client is null (expected in request mode)");
            }
            if (dynamoDbClient != null) {
                log.info("✅ AWS DynamoDb client initialized");
            } else {
                log.warn("⚠️  DynamoDb client is null (expected in request mode)");
            }
            log.info("🔗 AWS credentials loaded successfully");
        } catch (Exception e) {
            log.error("❌ AWS connection failed: {}", e.getMessage(), e);
        }
    }
}