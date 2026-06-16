package br.com.pedrogallonalves.tccmba.backend.controller;

import br.com.pedrogallonalves.tccmba.backend.model.ProcessingRequest;
import br.com.pedrogallonalves.tccmba.backend.service.ProcessingService;
import lombok.RequiredArgsConstructor;
import lombok.extern.slf4j.Slf4j;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

@Slf4j
@RestController
@RequestMapping("/api/v1/")
@RequiredArgsConstructor
@ConditionalOnProperty(name = "app.mode", havingValue = "request", matchIfMissing = false)
public class ProcessingController {

    private final ProcessingService processingService;

    @PostMapping("/process")
    public ResponseEntity<ProcessingRequest> processRequest(@RequestBody String data) {
        log.info("Received processing request with data: {}", data);
        ProcessingRequest result = processingService.processRequest(data);
        return ResponseEntity.status(HttpStatus.CREATED).body(result);
    }

    @GetMapping("/process/{key}")
    public ResponseEntity<String> getRequest(@PathVariable String key) {
        log.info("Getting process with key: {}", key);
        String result = processingService.getDynamoData(key);
        return ResponseEntity.status(HttpStatus.CREATED).body(result);
    }

}