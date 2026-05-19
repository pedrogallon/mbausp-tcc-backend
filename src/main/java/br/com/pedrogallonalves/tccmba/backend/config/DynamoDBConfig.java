package br.com.pedrogallonalves.tccmba.backend.config;


import lombok.extern.slf4j.Slf4j;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import software.amazon.awssdk.services.dynamodb.DynamoDbClient;

@Slf4j
@Configuration
public class DynamoDBConfig {

    @Bean
    public DynamoDbClient dynamoDbClient() {
        log.info("Initializing DynamoDB Client");
        return DynamoDbClient.builder().build();
    }
}