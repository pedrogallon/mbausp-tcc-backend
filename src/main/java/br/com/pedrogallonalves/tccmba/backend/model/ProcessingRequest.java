package br.com.pedrogallonalves.tccmba.backend.model;


import lombok.AllArgsConstructor;
import lombok.Builder;
import lombok.Data;
import lombok.NoArgsConstructor;
import software.amazon.awssdk.enhanced.dynamodb.mapper.annotations.DynamoDbBean;
import software.amazon.awssdk.enhanced.dynamodb.mapper.annotations.DynamoDbPartitionKey;
import java.time.LocalDateTime;
import java.util.UUID;

@Data
@NoArgsConstructor
@AllArgsConstructor
@Builder
@DynamoDbBean
public class ProcessingRequest {

    private String requestId;
    private String status;
    private String data;
    private String result;
    private String source;
    private LocalDateTime createdAt;
    private LocalDateTime processedAt;

    @DynamoDbPartitionKey
    public String getRequestId() {
        return requestId;
    }

    public static ProcessingRequest newRequest(String data, String source) {
        return ProcessingRequest.builder()
                .requestId(UUID.randomUUID().toString())
                .status("PENDING")
                .source(source)
                .data(data)
                .createdAt(LocalDateTime.now())
                .build();
    }
}