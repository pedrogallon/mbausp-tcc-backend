package br.com.pedrogallonalves.tccmba.backend.event.producer;

import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.SendMessageRequest;
import software.amazon.awssdk.services.sqs.model.SendMessageResponse;

@Slf4j
@Component
@RequiredArgsConstructor
public class SqsProducer {

    private final SqsClient sqsClient;

    @Value("${aws.sqs.queue-url}")
    private String queueUrl;

    public void sendMessage(String message) {
        try {
            SendMessageRequest sendMessageRequest = SendMessageRequest.builder()
                    .queueUrl(queueUrl)
                    .messageBody(message)
                    .build();

            SendMessageResponse response = sqsClient.sendMessage(sendMessageRequest);

            log.info("📤 Message sent to SQS successfully");
            log.info("   MessageId: {}", response.messageId());
            log.info("   Queue: {}", queueUrl);
        } catch (Exception e) {
            log.error("❌ Failed to send message to SQS", e);
            throw new RuntimeException("Failed to send SQS message", e);
        }
    }
}