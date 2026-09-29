package br.com.pedrogallonalves.tccmba.backend.event.listener;

import br.com.pedrogallonalves.tccmba.backend.service.ProcessingService;
import io.awspring.cloud.sqs.annotation.SqsListener;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.stereotype.Component;

@Slf4j
@Component
@RequiredArgsConstructor
@ConditionalOnProperty(name = "app.mode", havingValue = "event", matchIfMissing = false)
public class SqsProcessingListener {

    private final ProcessingService processingService;

    @SqsListener("${aws.sqs.queue-name}")
    public void listen(String message) {
        log.info("Received message: {}", message);
        processingService.processRequest(message, "events");
    }
}
