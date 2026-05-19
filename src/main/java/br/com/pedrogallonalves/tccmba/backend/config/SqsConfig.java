package br.com.pedrogallonalves.tccmba.backend.config;

import io.awspring.cloud.sqs.operations.SqsTemplate;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import software.amazon.awssdk.services.sqs.SqsAsyncClient;
import software.amazon.awssdk.services.sqs.SqsClient;

@Slf4j
@Configuration
@ConditionalOnProperty(name = "app.mode", havingValue = "event")
public class SqsConfig {

    @Bean
    public SqsClient sqsClient() {
        log.info("Initializing SqsClient");
        return SqsClient.builder().build();
    }

    @Bean
    public SqsAsyncClient sqsAsyncClient() {
        log.info("Initializing SqsAsyncClient - SQS polling ready");
        return SqsAsyncClient.builder().build();
    }

    @Bean
    public SqsTemplate sqsTemplate(SqsAsyncClient sqsAsyncClient) {
        log.info("Initializing SqsTemplate");
        return SqsTemplate.builder().sqsAsyncClient(sqsAsyncClient).build();
    }
}