package br.com.pedrogallonalves.tccmba.backend.service;

import br.com.pedrogallonalves.tccmba.backend.metrics.EmfProcessTimePublisher;
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
import java.time.LocalDateTime;
import java.util.Iterator;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;

@Slf4j
@Service
@RequiredArgsConstructor
public class ProcessingService {

    private final DynamoDbClient dynamoDbClient;
    private final MeterRegistry meterRegistry;
    private final EmfProcessTimePublisher emfProcessTimePublisher;
    private final ObjectMapper objectMapper = new ObjectMapper();
    private final ConcurrentHashMap<String, Timer> processingTimers = new ConcurrentHashMap<>();

    @Value("${aws.dynamodb.table-name}")
    private String TABLE_NAME;

    @Value("${app.processing.cpu-passes:5}")
    private int cpuPasses;

    @Value("${app.processing.enrichment-rounds:80}")
    private int enrichmentRounds;

    private Timer processingTimer(String source) {
        return processingTimers.computeIfAbsent(source, key -> Timer.builder(key + ".process.time")
                .description("Time taken to process a request/event")
                .publishPercentiles(0.5, 0.95, 0.99)
                .publishPercentileHistogram(true)
                .register(meterRegistry));
    }

    public ProcessingRequest processRequest(String inputData, String source) {
        log.debug("Starting processing (source={}, bytes={})", source, inputData != null ? inputData.length() : 0);

        meterRegistry.counter(String.format("%s.process.total", source)).increment();

        long startNanos = System.nanoTime();
        try {
            ProcessingRequest request = ProcessingRequest.newRequest(inputData, source);

            String fingerprint = simulateBusinessWork(inputData, source);

            request.setResult("Processed:" + fingerprint);
            request.setStatus("COMPLETED");
            request.setProcessedAt(LocalDateTime.now());

            saveRequestToDynamoDB(request);

            meterRegistry.counter(String.format("%s.process.success", source)).increment();
            log.debug("Request processed successfully: {} (source={})", request.getRequestId(), source);

            return request;
        } catch (Exception e) {
            meterRegistry.counter(String.format("%s.process.error", source)).increment();
            log.error("Error processing request", e);
            throw new RuntimeException("Error processing request", e);
        } finally {
            long elapsedNanos = System.nanoTime() - startNanos;
            processingTimer(source).record(elapsedNanos, TimeUnit.NANOSECONDS);
            emfProcessTimePublisher.record(source, elapsedNanos / 1_000_000.0);
        }
    }

    private String simulateBusinessWork(String inputData, String source) {
        int passes = Math.max(1, cpuPasses);
        int rounds = Math.max(1, enrichmentRounds);
        try {
            JsonNode root = objectMapper.readTree(inputData);
            long checksum = 0L;

            for (int pass = 0; pass < passes; pass++) {
                checksum = 31L * checksum + walkAndEnrich(root, source, pass, rounds);
                if (pass < passes - 1) {
                    String intermediate = objectMapper.writeValueAsString(root);
                    root = objectMapper.readTree(intermediate);
                }
            }

            return Long.toHexString(checksum);
        } catch (JsonProcessingException e) {
            throw new RuntimeException("Invalid JSON payload", e);
        }
    }

    private long walkAndEnrich(JsonNode node, String source, int pass, int rounds) {
        long hash = (source.hashCode() * 31L) + pass;

        if (node == null || node.isNull()) {
            return hash;
        }
        if (node.isObject()) {
            Iterator<Map.Entry<String, JsonNode>> fields = node.fields();
            while (fields.hasNext()) {
                Map.Entry<String, JsonNode> entry = fields.next();
                hash = 31L * hash + entry.getKey().toLowerCase().hashCode();
                hash = 31L * hash + walkAndEnrich(entry.getValue(), source, pass, rounds);
            }
            return hash;
        }
        if (node.isArray()) {
            for (JsonNode child : node) {
                hash = 31L * hash + walkAndEnrich(child, source, pass, rounds);
            }
            return hash;
        }
        if (node.isTextual()) {
            String normalized = node.asText().trim().toLowerCase();
            for (int i = 0; i < rounds; i++) {
                hash = 31L * hash + normalized.hashCode();
                hash = Long.rotateLeft(hash, (i + pass) % 17);
                hash ^= (normalized.length() + i) * 0x9E3779B97F4A7C15L;
            }
            return hash;
        }
        if (node.isNumber()) {
            double value = node.asDouble();
            for (int i = 0; i < rounds; i++) {
                hash = 31L * hash + Double.hashCode(value + i);
            }
            return hash;
        }
        return 31L * hash + node.hashCode();
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
            log.debug("Request saved to DynamoDB: {}", request.getRequestId());
        } catch (Exception e) {
            log.error("Failed to save to DynamoDB", e);
            throw new RuntimeException("Failed to save to DynamoDB", e);
        }
    }

    public String getDynamoData(String key) {
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
