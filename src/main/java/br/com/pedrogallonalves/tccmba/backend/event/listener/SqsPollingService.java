//package br.com.pedrogallonalves.tccmba.backend.event.listener;
//
//import service.br.com.pedrogallonalves.tcc.mba.backend.ProcessingService;
//import jakarta.annotation.PostConstruct;
//import jakarta.annotation.PreDestroy;
//import lombok.extern.slf4j.Slf4j;
//import org.springframework.beans.factory.annotation.Value;
//import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
//import org.springframework.stereotype.Component;
//import software.amazon.awssdk.services.sqs.SqsClient;
//import software.amazon.awssdk.services.sqs.model.DeleteMessageRequest;
//import software.amazon.awssdk.services.sqs.model.Message;
//import software.amazon.awssdk.services.sqs.model.ReceiveMessageRequest;
//
//import java.util.concurrent.ExecutorService;
//import java.util.concurrent.Executors;
//import java.util.concurrent.TimeUnit;
//
//@Slf4j
//@Component
//@ConditionalOnProperty(name = "app.mode", havingValue = "event", matchIfMissing = false)
//public class SqsPollingService {
//
//    private final SqsClient sqsClient;
//    private final ProcessingService processingService;
//
//    @Value("${aws.sqs.queue-url}")
//    private final String queueUrl;
//
//    private final ExecutorService executor = Executors.newSingleThreadExecutor();
//    private volatile boolean running = true;
//
//    public SqsPollingService(@org.springframework.beans.factory.annotation.Value("${aws.sqs.queue-url}") String queueUrl,
//                             SqsClient sqsClient,
//                             ProcessingService processingService) {
//        this.queueUrl = queueUrl;
//        this.sqsClient = sqsClient;
//        this.processingService = processingService;
//    }
//
//    @PostConstruct
//    public void start() {
//        log.info("SqsPollingService starting - polling queue: {}", queueUrl);
//        executor.submit(this::pollLoop);
//    }
//
//    private void pollLoop() {
//        while (running) {
//            try {
//                ReceiveMessageRequest req = ReceiveMessageRequest.builder()
//                        .queueUrl(queueUrl)
//                        .maxNumberOfMessages(5)
//                        .waitTimeSeconds(20) // long polling
//                        .visibilityTimeout(60)
//                        .build();
//
//                var resp = sqsClient.receiveMessage(req);
//                for (Message msg : resp.messages()) {
//                    try {
//                        log.info("SQS poll - received messageId={} body={}", msg.messageId(), msg.body());
//                        processingService.processRequest(msg.body());
//
//                        // delete after successful processing
//                        DeleteMessageRequest del = DeleteMessageRequest.builder()
//                                .queueUrl(queueUrl)
//                                .receiptHandle(msg.receiptHandle())
//                                .build();
//                        sqsClient.deleteMessage(del);
//                        log.info("SQS poll - deleted messageId={}", msg.messageId());
//                    } catch (Exception e) {
//                        log.error("Error processing or deleting message id={}", msg.messageId(), e);
//                        // do not delete so message becomes visible again after visibility timeout
//                    }
//                }
//            } catch (Exception e) {
//                log.error("Error receiving messages from SQS", e);
//                try {
//                    Thread.sleep(5000); // back off on errors
//                } catch (InterruptedException ie) {
//                    Thread.currentThread().interrupt();
//                }
//            }
//        }
//        log.info("SqsPollingService stopped polling");
//    }
//
//    @PreDestroy
//    public void stop() {
//        running = false;
//        executor.shutdown();
//        try {
//            if (!executor.awaitTermination(5, TimeUnit.SECONDS)) {
//                executor.shutdownNow();
//            }
//        } catch (InterruptedException e) {
//            executor.shutdownNow();
//            Thread.currentThread().interrupt();
//        }
//    }
//}