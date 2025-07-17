package p2p

import (
	"bufio"
	"fmt" // Explicitly used for fmt.Sprintf
	"log" // Explicitly used for log.Printf
	"os/exec"
	"regexp"
	"strings"
	"sync" // Explicitly used for sync.RWMutex
	"time"
)

// SerfMember represents a member in the Serf cluster, matching `serf members -format=json` output.
type SerfMember struct {
	Name     string                 `json:"name"`
	Addr     string                 `json:"addr"`
	Port     int                    `json:"port"`
	Tags     map[string]interface{} `json:"tags"`
	Status   string                 `json:"status"` // "alive", "failed", "left"
	Protocol map[string]int         `json:"protocol"`
}

// ParsedSerfEvent is a struct to hold the parsed user event data
type ParsedSerfEvent struct {
	Name      string
	PayloadHex string // Raw hex string of the payload
}

// StartSerfMonitor runs the 'serf monitor' command and streams its output.
// It parses user events and sends them to the provided channel.
// It updates metrics related to its own status (SerfMonitorStatus, SerfMonitorLastError, SerfRPCStatus)
// by acquiring the global metricsLock from the main package.
func StartSerfMonitor(
	serfExecutablePath string,
	serfRPCAddr string,
	eventChan chan<- ParsedSerfEvent, // Channel to send parsed events to main.go
	metricsLock *sync.RWMutex, // Pointer to the RWMutex in main
	serfMonitorStatus *string, // Pointer to global string in main
	serfMonitorLastError *string, // Pointer to global string in main
	serfRPCStatus *string, // Pointer to global string in main
) {
	log.Println("INFO: (p2p/serf_adapter) StartSerfMonitor goroutine started.")

	const (
		MONITOR_RESTART_DELAY = 5 * time.Second // Delay before restarting monitor process
	)

	// Regex to match 'Name:' and 'Payload:' lines within an event info block
	nameLineRe := regexp.MustCompile(`^\s*Name:\s*"?([^"]+)"?\s*$`)
	payloadLineRe := regexp.MustCompile(`^\s*Payload:\s*\[\]byte\{0x([0-9a-fA-F, ]+)\}$`)
	eventLogLineRe := regexp.MustCompile(`^\s*\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \[INFO\] agent: Received event: user-event: (.+)$`)

	var currentEventName string
	var currentEventPayloadHex string
	var inPotentialEventInfoBlock bool // State variable to track if we're in an event info multi-line block

	for { // Infinite loop to keep the monitor subprocess alive and restart if it exits
		metricsLock.Lock()
		*serfRPCStatus = "Connected" // Assume connected if trying to start monitor
		*serfMonitorStatus = "Running"
		metricsLock.Unlock()

		cmdArgsMonitor := []string{serfExecutablePath, "monitor", "-rpc-addr=" + serfRPCAddr}
		process := exec.Command(cmdArgsMonitor[0], cmdArgsMonitor[1:]...)
		stdout, err := process.StdoutPipe()
		if err != nil {
			log.Printf("ERROR: (p2p/serf_adapter) Failed to get stdout pipe for serf monitor: %v", err)
			metricsLock.Lock()
			*serfMonitorStatus = "Pipe Error"
			*serfMonitorLastError = fmt.Sprintf("Stdout Pipe Error: %v", err)
			metricsLock.Unlock()
			time.Sleep(MONITOR_RESTART_DELAY)
			continue
		}
		stderr, err := process.StderrPipe()
		if err != nil {
			log.Printf("ERROR: (p2p/serf_adapter) Failed to get stderr pipe for serf monitor: %v", err)
			metricsLock.Lock()
			*serfMonitorStatus = "Pipe Error"
			*serfMonitorLastError = fmt.Sprintf("Stderr Pipe Error: %v", err)
			metricsLock.Unlock()
			time.Sleep(MONITOR_RESTART_DELAY)
			continue
		}

		if err := process.Start(); err != nil {
			log.Printf("ERROR: (p2p/serf_adapter) Failed to start serf monitor command: %v", err)
			metricsLock.Lock()
			*serfMonitorStatus = "Command Start Error"
			*serfMonitorLastError = fmt.Sprintf("Command Start: %v", err)
			metricsLock.Unlock()
			time.Sleep(MONITOR_RESTART_DELAY)
			continue
		}

		log.Println("INFO: (p2p/serf_adapter) Serf monitor subprocess launched. Listening for ALL events (plain text format)...")

		// Goroutine to read stderr from serf monitor (for command errors)
		go func() {
			errScanner := bufio.NewScanner(stderr)
			for errScanner.Scan() {
				errLine := errScanner.Text()
				log.Printf("ERROR: (p2p/serf_adapter) Serf monitor stderr: %s", errLine)
				metricsLock.Lock()
				*serfMonitorLastError = errLine // Update global error metric
				metricsLock.Unlock()
			}
		}()

		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			line := scanner.Text()
			if line == "" {
				continue
			}

			log.Printf("DEBUG: (p2p/serf_adapter) Received raw Serf monitor line: %s", line)

			metricsLock.Lock()
			*serfRPCStatus = "Connected" // If we're getting output, Serf RPC is reachable
			metricsLock.Unlock()

			// --- Robust Multi-line parsing logic for Serf user events ---
			// This logic prioritizes parsing explicit "Name:" and "Payload:" lines.
			// The state `inPotentialEventInfoBlock` helps group these lines.
			if nameMatch := nameLineRe.FindStringSubmatch(line); len(nameMatch) > 1 {
				inPotentialEventInfoBlock = true // Start a potential event info block
				currentEventName = nameMatch[1]
				currentEventPayloadHex = "" // Reset payload for new event
			} else if payloadMatch := payloadLineRe.FindStringSubmatch(line); len(payloadMatch) > 1 {
				if inPotentialEventInfoBlock { // Only process if we are in an event info block
					currentEventPayloadHex = strings.ReplaceAll(payloadMatch[1], " ", "") // Extract hex string, remove spaces, 0x, and commas
					
					// If we have a name and payload, send the event to the channel
					if currentEventName != "" && currentEventPayloadHex != "" {
						eventChan <- ParsedSerfEvent{Name: currentEventName, PayloadHex: currentEventPayloadHex}
						log.Printf("INFO: (p2p/serf_adapter) Dispatched parsed event to channel: '%s'", currentEventName)
						// Reset state for next event
						inPotentialEventInfoBlock = false
						currentEventName = ""
						currentEventPayloadHex = ""
					} else {
						log.Printf("DEBUG: (p2p/serf_adapter) Payload line found, but Name not yet, or other issue in block: %s", line)
					}
				}
			} else if eventLogMatch := eventLogLineRe.FindStringSubmatch(line); len(eventLogMatch) > 1 {
				// This catches the standard "Received event: user-event: <name>" line.
				// We'll process it only if we're not already in a multi-line info block
				// that should have provided Name/Payload more explicitly.
				if !inPotentialEventInfoBlock {
					eventName := eventLogMatch[1]
					log.Printf("INFO: (p2p/serf_adapter) Parsed Serf user event (simple line): Name='%s'", eventName)
					// No payload to dispatch from this line format, so we don't send to channel.
					
					inPotentialEventInfoBlock = false // Reset state
					currentEventName = ""
					currentEventPayloadHex = ""
				}
			} else { // Any other line
				if inPotentialEventInfoBlock {
					log.Printf("DEBUG: (p2p/serf_adapter) Line ignored in event info block: %s", line)
				} else {
					// General Serf monitor log lines
					if strings.Contains(line, "[INFO] agent:") || strings.Contains(line, "[INFO] serf:") {
						log.Printf("DEBUG: (p2p/serf_adapter) Serf Agent/Internal Log: %s", line)
					} else {
						log.Printf("DEBUG: (p2p/serf_adapter) Other Serf monitor line (ignored): %s", line)
					}
				}
				// Reset block state if a non-event-info line (like another INFO log)
				// appears after a Name/Payload line, indicating the event block is over.
				if !strings.HasPrefix(line, "Name:") && !strings.HasPrefix(line, "Payload:") && !strings.Contains(line, "Received event: user-event:") && !strings.Contains(line, "Event Info:") && !strings.Contains(line, "Coalesce:") && !strings.Contains(line, "Event:") && !strings.Contains(line, "LTime:") {
					inPotentialEventInfoBlock = false
					currentEventName = ""
					currentEventPayloadHex = ""
				}
			}
		} // end for scanner.Scan()

		if err := scanner.Err(); err != nil {
			log.Printf("ERROR: (p2p/serf_adapter) Error reading serf monitor output: %v", err)
			metricsLock.Lock()
			*serfMonitorStatus = "Read Error"
			*serfMonitorLastError = fmt.Sprintf("Monitor Read Error: %v", err)
			metricsLock.Unlock()
		}

		process.Wait() // Wait for the subprocess to fully terminate
		if process.ProcessState.ExitCode() != 0 {
			log.Printf("ERROR: (p2p/serf_adapter) Serf monitor command exited with non-zero status: %d", process.ProcessState.ExitCode())
			metricsLock.Lock()
			*serfMonitorStatus = "Exited with Error"
			*serfMonitorLastError = fmt.Sprintf("CLI Exit Code: %d", process.ProcessState.ExitCode())
			metricsLock.Unlock()
		} else {
			log.Println("INFO: (p2p/serf_adapter) Serf monitor command exited gracefully.")
			metricsLock.Lock()
			*serfMonitorStatus = "Exited Gracefully"
			*serfMonitorLastError = ""
			metricsLock.Unlock()
		}

		time.Sleep(MONITOR_RESTART_DELAY) // Wait before restarting monitor process
	} // end for infinite loop
}

func min(a, b int) int {
    if a < b {
        return a
    }
    return b
}
