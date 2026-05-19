package br.com.pedrogallonalves.tccmba.backend.service;
import br.com.pedrogallonalves.tccmba.backend.model.ProcessingRequest;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import software.amazon.awssdk.enhanced.dynamodb.DynamoDbEnhancedClient;
import software.amazon.awssdk.enhanced.dynamodb.DynamoDbTable;
import software.amazon.awssdk.enhanced.dynamodb.TableSchema;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;

import java.time.LocalDateTime;

@Slf4j
@Service
@RequiredArgsConstructor
public class ProcessingService {

    private final DynamoDbClient dynamoDbClient;
    private final MeterRegistry meterRegistry;

    @Value("${aws.dynamodb.table-name}")
    private String TABLE_NAME;

    public ProcessingRequest processRequest(String inputData) {
        log.info("Starting processing for input: {}", inputData);

        Timer timer = Timer.builder("backend.request.processing.duration")
                .description("Time taken to process a request")
                .publishPercentiles(0.5, 0.95, 0.99)
                .register(meterRegistry);

        return timer.record(() -> {
            try {
                // Create new request
                ProcessingRequest request = ProcessingRequest.newRequest(inputData);

                // Simulate 1-second processing
                Thread.sleep(1000);

                // Process data
                request.setResult("Processed: " + inputData.toUpperCase());
                request.setStatus("COMPLETED");
                request.setProcessedAt(LocalDateTime.now());

                // Save to DynamoDB
                saveRequestToDynamoDB(request);

                meterRegistry.counter("backend.request.processed.success").increment();
                log.info("Request processed successfully: {}", request.getRequestId());

                return request;
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                meterRegistry.counter("backend.request.processed.error").increment();
                log.error("Processing interrupted", e);
                throw new RuntimeException("Processing interrupted", e);
            } catch (Exception e) {
                meterRegistry.counter("backend.request.processed.error").increment();
                log.error("Error processing request", e);
                throw new RuntimeException("Error processing request", e);
            }
        });
    }

    private void saveRequestToDynamoDB(ProcessingRequest request) {
        try {
            DynamoDbEnhancedClient enhancedClient = DynamoDbEnhancedClient.builder()
                    .dynamoDbClient(dynamoDbClient)
                    .build();

            DynamoDbTable<ProcessingRequest> table = enhancedClient.table(
                    TABLE_NAME,
                    TableSchema.fromBean(ProcessingRequest.class)
            );

            table.putItem(request);
            log.info("Request saved to DynamoDB: {}", request.getRequestId());
        } catch (Exception e) {
            log.error("Failed to save to DynamoDB", e);
            throw new RuntimeException("Failed to save to DynamoDB", e);
        }
    }

    public String getDynamoData(String key){
        try {
            DynamoDbEnhancedClient enhancedClient = DynamoDbEnhancedClient.builder()
                    .dynamoDbClient(dynamoDbClient)
                    .build();

            DynamoDbTable<ProcessingRequest> table = enhancedClient.table(
                    TABLE_NAME,
                    TableSchema.fromBean(ProcessingRequest.class)
            );

            return table.getItem(r -> r.key(k -> k.partitionValue(key))).toString();
        } catch (Exception e) {
            log.error("Failed to save to DynamoDB", e);
            throw new RuntimeException("Failed to save to DynamoDB", e);
        }
    }
}