package br.com.pedrogallonalves.tccmba.backend.metrics;

import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import lombok.extern.slf4j.Slf4j;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import software.amazon.cloudwatchlogs.emf.exception.DimensionSetExceededException;
import software.amazon.cloudwatchlogs.emf.exception.InvalidDimensionException;
import software.amazon.cloudwatchlogs.emf.exception.InvalidMetricException;
import software.amazon.cloudwatchlogs.emf.exception.InvalidNamespaceException;
import software.amazon.cloudwatchlogs.emf.logger.MetricsLogger;
import software.amazon.cloudwatchlogs.emf.model.DimensionSet;
import software.amazon.cloudwatchlogs.emf.model.Unit;

import java.util.ArrayList;
import java.util.List;
import java.util.Queue;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

/**
 * Publishes per-call process.time samples via CloudWatch Embedded Metric Format.
 * Individual samples allow CloudWatch extended statistics (p50/p95/p99).
 * Values are buffered (max 100 per EMF flush) to sustain high RPS.
 */
@Slf4j
@Component
public class EmfProcessTimePublisher {

    private static final int MAX_VALUES_PER_FLUSH = 100;

    private final ConcurrentHashMap<String, ConcurrentLinkedQueue<Double>> buffers = new ConcurrentHashMap<>();
    private ScheduledExecutorService scheduler;

    @Value("${app.emf.enabled:true}")
    private boolean enabled;

    @Value("${app.emf.namespace:TccMba}")
    private String namespace;

    @Value("${app.emf.flush-interval-ms:1000}")
    private long flushIntervalMs;

    @PostConstruct
    void start() {
        if (!enabled) {
            log.info("EMF process.time publisher disabled");
            return;
        }
        scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "emf-process-time-flusher");
            t.setDaemon(true);
            return t;
        });
        scheduler.scheduleAtFixedRate(this::flushAllSafe, flushIntervalMs, flushIntervalMs, TimeUnit.MILLISECONDS);
        log.info("EMF process.time publisher enabled (namespace={}, flushIntervalMs={})", namespace, flushIntervalMs);
    }

    public void record(String source, double durationMs) {
        if (!enabled || !Double.isFinite(durationMs) || durationMs < 0) {
            return;
        }
        ConcurrentLinkedQueue<Double> queue = buffers.computeIfAbsent(source, key -> new ConcurrentLinkedQueue<>());
        queue.add(durationMs);
        if (queue.size() >= MAX_VALUES_PER_FLUSH) {
            flushSource(source);
        }
    }

    @PreDestroy
    void shutdown() {
        if (scheduler != null) {
            scheduler.shutdown();
            try {
                scheduler.awaitTermination(2, TimeUnit.SECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
        }
        flushAll();
    }

    private void flushAllSafe() {
        try {
            flushAll();
        } catch (Exception e) {
            log.warn("EMF periodic flush failed: {}", e.toString());
        }
    }

    private void flushAll() {
        for (String source : buffers.keySet()) {
            flushSource(source);
        }
    }

    private void flushSource(String source) {
        Queue<Double> queue = buffers.get(source);
        if (queue == null || queue.isEmpty()) {
            return;
        }

        while (!queue.isEmpty()) {
            List<Double> batch = new ArrayList<>(MAX_VALUES_PER_FLUSH);
            Double value;
            while (batch.size() < MAX_VALUES_PER_FLUSH && (value = queue.poll()) != null) {
                batch.add(value);
            }
            if (batch.isEmpty()) {
                return;
            }
            publishBatch(source, batch);
        }
    }

    private void publishBatch(String source, List<Double> batch) {
        MetricsLogger metricsLogger = new MetricsLogger();
        try {
            metricsLogger.setNamespace(namespace);
            metricsLogger.resetDimensions(false);
            metricsLogger.setDimensions(DimensionSet.of("Source", source));
            for (Double durationMs : batch) {
                metricsLogger.putMetric("process.time", durationMs, Unit.MILLISECONDS);
            }
            metricsLogger.flush();
        } catch (InvalidNamespaceException | InvalidDimensionException | DimensionSetExceededException
                 | InvalidMetricException e) {
            log.warn("EMF publish rejected (source={}, n={}): {}", source, batch.size(), e.toString());
        } catch (RuntimeException e) {
            log.warn("EMF publish failed (source={}, n={}): {}", source, batch.size(), e.toString());
        }
    }
}
