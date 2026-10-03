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

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.charset.StandardCharsets;
import java.time.LocalDateTime;
import java.util.Random;

@Slf4j
@Service
@RequiredArgsConstructor
public class ProcessingService {

    private final DynamoDbClient dynamoDbClient;
    private final MeterRegistry meterRegistry;
    private final ObjectMapper objectMapper = new ObjectMapper();
    private final Random random = new Random();

    @Value("${aws.dynamodb.table-name}")
    private String TABLE_NAME;

    public ProcessingRequest processRequest(String inputData, String source) {
        log.info("Starting processing for input: {} (source={})", inputData, source);

        meterRegistry.counter(String.format("tcc.%s.processed.total", source)).increment();

        Timer timer = Timer.builder(String.format("tcc.%s.processing.time", source))
                .description("Time taken to process a request/event")
                .publishPercentiles(0.5, 0.95, 0.99)
                .publishPercentileHistogram(true)
                .register(meterRegistry);

        return timer.record(() -> {
            try {
                ProcessingRequest request = ProcessingRequest.newRequest(inputData, source);

                simulateCpuWork(inputData, source);

                request.setResult("Processed: " + inputData.toUpperCase());
                request.setStatus("COMPLETED");
                request.setProcessedAt(LocalDateTime.now());

                saveRequestToDynamoDB(request);

                meterRegistry.counter(String.format("tcc.%s.processed.success", source)).increment();
                log.info("Request processed successfully: {} (source={})", request.getRequestId(), source);

                return request;
            } catch (Exception e) {
                meterRegistry.counter(String.format("tcc.%s.processed.error", source)).increment();
                log.error("Error processing request", e);
                throw new RuntimeException("Error processing request", e);
            }
        });
    }

    private void simulateCpuWork(String inputData, String source) {
        int iterations = 1000;
        for (int i = 0; i < iterations; i++) {
            String json = buildJsonPayload(inputData, source, i);
            try {
                JsonNode node = objectMapper.readTree(json);
                String normalized = objectMapper.writeValueAsString(node);
                if (normalized.length() > 0 && normalized.charAt(0) == '{') {
                    int checksum = normalized.hashCode();
                    if ((checksum & 1) == 0) {
                        normalized = normalized.toUpperCase();
                    }
                }
            } catch (JsonProcessingException ignored) {
                // CPU-bound JSON work only; no external I/O
            }
        }
    }

    private String buildJsonPayload(String inputData, String source, int index) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"source\":\"").append(source)
                .append("\",\"index\":").append(index)
                .append(",\"payload\":\"").append(inputData)
                .append("\",\"checksum\":\"").append(Integer.toHexString(Math.abs(random.nextInt())))
                .append("\",\"nested\":{");

        for (int i = 0; i < 14; i++) {
            sb.append("\"k").append(i).append("\":\"")
                    .append(new String(randomBytes(64), StandardCharsets.UTF_8))
                    .append("\",");
        }
        sb.append("\"final\":\"").append(inputData.toUpperCase()).append("\"}} ");
        return sb.toString();
    }

    private byte[] randomBytes(int length) {
        byte[] bytes = new byte[length];
        for (int i = 0; i < length; i++) {
            bytes[i] = (byte) (random.nextInt(26) + 'a');
        }
        return bytes;
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