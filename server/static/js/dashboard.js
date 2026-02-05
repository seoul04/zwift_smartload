/**
 * Dashboard JavaScript for Zwift Data Visualization
 */

// Configuration
const UPDATE_INTERVAL = 1000; // milliseconds
const API_BASE = '';

// Metrics configuration (loaded from server)
let METRIC_CONFIG = {};

let chartData = {
    data: [],
    layout: null,
    config: {responsive: true, displayModeBar: true}
};

let currentWindow = 5;

// Initialize
document.addEventListener('DOMContentLoaded', async function() {
    // Check if Plotly is loaded
    if (typeof Plotly === 'undefined') {
        console.error('Plotly library not loaded!');
        document.getElementById('plotly-chart').innerHTML = 
            '<div style="color: #f44336; padding: 20px; text-align: center;">Error: Plotly library failed to load. Please check your internet connection.</div>';
        return;
    }
    
    try {
        // Load metrics configuration from server
        await loadMetricsConfig();
        
        // Generate current values HTML dynamically
        generateCurrentValuesHTML();
        
        // Clear loading message
        const chartElement = document.getElementById('plotly-chart');
        if (chartElement) {
            chartElement.classList.remove('loading');
            chartElement.innerHTML = ''; // Clear loading text
        }
        
        // Initialize chart and UI with loaded config
        initializeChart();
        setupEventListeners();
        startDataUpdates();
        startStatusUpdates();
    } catch (error) {
        console.error('Error initializing dashboard:', error);
        const chartElement = document.getElementById('plotly-chart');
        if (chartElement) {
            chartElement.classList.remove('loading');
            chartElement.innerHTML = 
                '<div style="color: #f44336; padding: 20px; text-align: center;">Error initializing chart: ' + error.message + '</div>';
        }
    }
});

async function loadMetricsConfig() {
    try {
        const response = await fetch(`${API_BASE}/api/metrics-config`);
        if (!response.ok) {
            throw new Error(`Failed to load metrics config: ${response.status}`);
        }
        const result = await response.json();
        METRIC_CONFIG = result.metrics_config || {};
        console.log('Metrics configuration loaded:', Object.keys(METRIC_CONFIG).length, 'metrics');
        
        // Log ranges for debugging
        Object.keys(METRIC_CONFIG).forEach(metric => {
            const config = METRIC_CONFIG[metric];
            if (config.yaxis_range) {
                console.log(`${metric} (${config.yaxis}): range =`, config.yaxis_range);
            }
        });
    } catch (error) {
        console.error('Error loading metrics config:', error);
        throw error;
    }
}

function generateCurrentValuesHTML() {
    const currentValuesContainer = document.getElementById('current-values');
    if (!currentValuesContainer) {
        console.error('Current values container not found');
        return;
    }
    
    // Clear existing content
    currentValuesContainer.innerHTML = '';
    
    // Get metrics that should be shown in current values
    const currentValueMetrics = Object.keys(METRIC_CONFIG)
        .filter(metric => METRIC_CONFIG[metric].show_in_current_values !== false)
        .sort((a, b) => {
            // Sort by display name for consistent ordering
            const nameA = METRIC_CONFIG[a].display_name || a;
            const nameB = METRIC_CONFIG[b].display_name || b;
            return nameA.localeCompare(nameB);
        });
    
    // Generate HTML for each metric
    currentValueMetrics.forEach(metric => {
        const config = METRIC_CONFIG[metric];
        const displayName = config.display_name || metric;
        const unit = config.unit || '';
        const color = config.color || '#888888';
        
        const valueItem = document.createElement('div');
        valueItem.className = 'value-item';
        
        valueItem.innerHTML = `
            <span class="value-label">${displayName}</span>
            <div class="value-display">
                <span class="value-indicator" style="background: ${color};"></span>
                <span class="value-number" id="value-${metric}">--</span>
                <span class="value-unit">${unit}</span>
            </div>
        `;
        
        currentValuesContainer.appendChild(valueItem);
    });
    
    console.log('Generated current values HTML for', currentValueMetrics.length, 'metrics');
}

function initializeChart() {
    // Filter metrics that should be shown in plot
    const plotMetrics = Object.keys(METRIC_CONFIG)
        .filter(metric => METRIC_CONFIG[metric].show_in_plot !== false);
    
    console.log('Plot metrics:', plotMetrics);
    
    const traces = plotMetrics.map(metric => {
        const config = METRIC_CONFIG[metric];
        
        // Build line configuration
        const lineConfig = {
            color: config.color,
            width: config.line_width || 2,
            shape: 'hv'  // Staircase: horizontal then vertical (step after)
        };
        
        // Add dash style if not solid
        if (config.line_style && config.line_style !== 'solid') {
            // Plotly dash styles: 'solid', 'dash', 'dot', 'dashdot', 'longdash', 'longdashdot'
            lineConfig.dash = config.line_style;
        }
        
        return {
            x: [],
            y: [],
            name: config.display_name,
            type: 'scatter',
            mode: 'lines',
            line: lineConfig,
            yaxis: config.yaxis,
            visible: true
        };
    });
    
    console.log('Created traces:', traces.map(t => t.name));
    
    // Build y-axes dynamically from config
    const yaxes = {};
    // Y-axis positions - evenly spaced based on actual usage
    // Left side: y, y7, y9 (3 axes)
    // Right side: y2, y3, y4, y6 (4 axes)
    const yaxisPositions = {
        'y': {side: 'left', position: 0.0},      // Heart Rate
        'y2': {side: 'right', position: 0.2},      // Power
        'y3': {side: 'right', position: 0.4},    // Cadence
        'y4': {side: 'right', position: 0.6},     // Speed
        'y6': {side: 'right', position: 0.8},     // Grade
        'y7': {side: 'left', position: 0.2},      // Resistance
        'y9': {side: 'left', position: 0.4}      // Releasing
    };
    
    // Collect unique y-axes from plot metrics
    // Group metrics by y-axis to find best title and range
    const yaxisGroups = {};
    plotMetrics.forEach(metric => {
        const yaxis = METRIC_CONFIG[metric].yaxis;
        if (yaxis) {
            if (!yaxisGroups[yaxis]) {
                yaxisGroups[yaxis] = [];
            }
            yaxisGroups[yaxis].push(metric);
        }
    });
    
    // Sort y-axes to ensure consistent ordering
    const sortedYaxes = Object.keys(yaxisGroups).sort((a, b) => {
        // Extract number from y-axis name (y, y2, y3, etc.)
        const numA = a === 'y' ? 0 : parseInt(a.substring(1)) || 999;
        const numB = b === 'y' ? 0 : parseInt(b.substring(1)) || 999;
        return numA - numB;
    });
    
    // Build y-axes from grouped metrics with proper positioning
    sortedYaxes.forEach(yaxis => {
        const metrics = yaxisGroups[yaxis];
        const firstConfig = METRIC_CONFIG[metrics[0]];
        const pos = yaxisPositions[yaxis] || {side: 'right', position: 0.5};
        
        // Find a metric with a range, or use the first one
        let rangeConfig = null;
        for (const metric of metrics) {
            if (METRIC_CONFIG[metric].yaxis_range) {
                rangeConfig = METRIC_CONFIG[metric].yaxis_range;
                break;
            }
        }
        
        // Build a title - if multiple metrics share axis, use a generic title
        let title;
        if (metrics.length === 1) {
            title = `${firstConfig.display_name} (${firstConfig.unit})`;
        } else {
            // Multiple metrics - use unit if all share the same unit, otherwise generic
            const units = [...new Set(metrics.map(m => METRIC_CONFIG[m].unit))];
            if (units.length === 1 && units[0]) {
                title = `(${units[0]})`;
            } else {
                title = ''; // Generic axis
            }
        }
        
        yaxes[yaxis] = {
            title: title,
            color: firstConfig.color,
            gridcolor: '#3a3a3a',
            side: pos.side,
            position: pos.position,
            overlaying: yaxis === 'y' ? undefined : 'y'
        };
        
        console.log(`Y-axis ${yaxis}: side=${pos.side}, position=${pos.position}`);
        
        // Add range if specified, and disable autorange
        if (rangeConfig) {
            // Ensure range is [min, max] format
            const min = Math.min(rangeConfig[0], rangeConfig[1]);
            const max = Math.max(rangeConfig[0], rangeConfig[1]);
            yaxes[yaxis].range = [min, max];
            yaxes[yaxis].autorange = false;
            console.log(`Y-axis ${yaxis} range set to [${min}, ${max}]`);
        } else {
            console.log(`Y-axis ${yaxis} has no range specified`);
        }
    });
    
    // Convert y-axis keys to Plotly format (y -> yaxis, y2 -> yaxis2, etc.)
    const plotlyYaxes = {};
    Object.keys(yaxes).forEach(key => {
        const plotlyKey = key === 'y' ? 'yaxis' : `yaxis${key.substring(1)}`;
        plotlyYaxes[plotlyKey] = yaxes[key];
    });
    
    // Log final y-axes configuration
    console.log('Y-axes configuration:', Object.keys(plotlyYaxes).map(y => {
        const axis = plotlyYaxes[y];
        return `${y}: side=${axis.side}, position=${axis.position}, range=${axis.range || 'auto'}, title="${axis.title}"`;
    }));
    
    chartData.layout = {
        title: {
            text: 'Real-time Sensor Data',
            font: {color: '#e0e0e0', size: 20}
        },
        xaxis: {
            title: 'Time',
            color: '#b0b0b0',
            gridcolor: '#3a3a3a',
            showgrid: true
        },
        ...plotlyYaxes,
        plot_bgcolor: '#2a2a2a',
        paper_bgcolor: '#1a1a1a',
        font: {color: '#e0e0e0'},
        showlegend: true,
        legend: {
            x: 1.1,
            y: 1,
            bgcolor: 'rgba(42, 42, 42, 0.8)',
            bordercolor: '#4a4a4a',
            borderwidth: 1
        },
        margin: {l: 80, r: 200, t: 60, b: 60}
    };
    
    chartData.data = traces;
    
    try {
        const chartElement = document.getElementById('plotly-chart');
        if (chartElement) {
            // Clear any loading message
            chartElement.classList.remove('loading');
            chartElement.innerHTML = '';
        }
        
        Plotly.newPlot('plotly-chart', chartData.data, chartData.layout, chartData.config);
        console.log('Chart initialized successfully with', traces.length, 'traces');
        console.log('Trace names:', traces.map(t => t.name));
    } catch (error) {
        console.error('Error creating Plotly chart:', error);
        throw error;
    }
}

function setupEventListeners() {
    // Time window selector
    document.getElementById('time-window').addEventListener('change', function(e) {
        currentWindow = parseInt(e.target.value);
        updateChart();
    });
}

function startDataUpdates() {
    updateChart();
    setInterval(updateChart, UPDATE_INTERVAL);
}

function startStatusUpdates() {
    updateStatus();
    setInterval(updateStatus, 2000); // Update status every 2 seconds
}

async function updateChart() {
    try {
        const response = await fetch(`${API_BASE}/api/data?window=${currentWindow}`);
        
        if (!response.ok) {
            console.error('API request failed:', response.status, response.statusText);
            return;
        }
        
        const result = await response.json();
        
        if (result.error) {
            console.error('API error:', result.error);
            return;
        }
        
        const data = result.data;
        
        // Debug: log data structure
        console.log('Received data:', Object.keys(data).length, 'metrics');
        Object.keys(data).forEach(metric => {
            if (data[metric] && data[metric].timestamps) {
                console.log(`${metric}: ${data[metric].timestamps.length} points`);
            }
        });
        const updates = {
            x: [],
            y: []
        };
        
        const indices = [];
        
        // Only update metrics that are in the plot (same order as initialization)
        const plotMetrics = Object.keys(METRIC_CONFIG)
            .filter(metric => METRIC_CONFIG[metric].show_in_plot !== false);
        
        plotMetrics.forEach((metric, index) => {
            if (data[metric] && data[metric].timestamps && data[metric].values) {
                updates.x.push(data[metric].timestamps);
                updates.y.push(data[metric].values);
                indices.push(index);
            } else {
                // No data for this metric
                updates.x.push([]);
                updates.y.push([]);
                indices.push(index);
            }
        });
        
        if (indices.length > 0) {
            Plotly.update('plotly-chart', updates, {}, indices);
        }
        
        // Update current values display
        updateCurrentValues(data);
        
    } catch (error) {
        console.error('Error updating chart:', error);
    }
}

function updateCurrentValues(data) {
    Object.keys(METRIC_CONFIG).forEach(metric => {
        const valueElement = document.getElementById(`value-${metric}`);
        if (valueElement) {
            if (data[metric] && data[metric].values && data[metric].values.length > 0) {
                // Get the most recent value
                const latestValue = data[metric].values[data[metric].values.length - 1];
                const config = METRIC_CONFIG[metric];
                
                // Format the value based on type and unit
                let formattedValue;
                if (typeof latestValue === 'number') {
                    const unit = config.unit || '';
                    
                    // Format based on unit type
                    if (unit === '') {
                        // Unitless values (coefficients) - show 2 decimal places
                        formattedValue = latestValue.toFixed(2);
                    } else if (unit === '%') {
                        // Percentages - show 1 decimal place
                        formattedValue = latestValue.toFixed(1);
                    } else {
                        // For metrics with units, show as integer if whole number, else 1 decimal
                        if (latestValue % 1 === 0) {
                            formattedValue = Math.round(latestValue).toString();
                        } else {
                            formattedValue = Math.round(latestValue * 10) / 10;
                        }
                    }
                } else {
                    formattedValue = latestValue;
                }
                
                valueElement.textContent = formattedValue;
            } else {
                valueElement.textContent = '--';
            }
        }
    });
}

function getRSSIClass(rssi) {
    if (rssi === null || rssi === undefined) {
        return 'disconnected';
    }
    if (rssi >= -50) {
        return 'excellent';
    } else if (rssi >= -70) {
        return 'good';
    } else if (rssi >= -85) {
        return 'weak';
    } else {
        return 'very-weak';
    }
}

function formatDeviceName(device) {
    const names = {
        'hr': 'HR',
        'cp': 'Power Meter',
        'ftms': 'Trainer'
    };
    return names[device] || device.toUpperCase();
}

async function updateStatus() {
    try {
        const response = await fetch(`${API_BASE}/api/status`);
        const result = await response.json();
        
        const indicator = document.getElementById('status-indicator');
        const statusText = document.getElementById('status-text');
        
        if (result.connected) {
            indicator.classList.add('connected');
            statusText.textContent = `Connected to ${result.port || 'serial port'}`;
        } else {
            indicator.classList.remove('connected');
            statusText.textContent = `Disconnected from ${result.port || 'serial port'}`;
        }
        
        // Update device status
        const deviceStatusContainer = document.getElementById('device-status');
        if (deviceStatusContainer && result.devices) {
            deviceStatusContainer.innerHTML = '';
            
            const deviceOrder = ['hr', 'cp', 'ftms'];
            deviceOrder.forEach(device => {
                const deviceInfo = result.devices[device];
                if (deviceInfo) {
                    const deviceItem = document.createElement('div');
                    deviceItem.className = 'device-item';
                    
                    const indicator = document.createElement('div');
                    indicator.className = 'device-indicator';
                    if (deviceInfo.connected && deviceInfo.rssi !== null) {
                        indicator.classList.add(getRSSIClass(deviceInfo.rssi));
                    } else {
                        indicator.classList.add('disconnected');
                    }
                    
                    const name = document.createElement('span');
                    name.className = 'device-name';
                    name.textContent = formatDeviceName(device);
                    
                    const rssi = document.createElement('span');
                    rssi.className = 'device-rssi';
                    if (deviceInfo.connected && deviceInfo.rssi !== null) {
                        rssi.textContent = `${deviceInfo.rssi} dBm`;
                    } else {
                        rssi.textContent = '--';
                    }
                    
                    deviceItem.appendChild(indicator);
                    deviceItem.appendChild(name);
                    deviceItem.appendChild(rssi);
                    deviceStatusContainer.appendChild(deviceItem);
                }
            });
        }
    } catch (error) {
        console.error('Error updating status:', error);
        const indicator = document.getElementById('status-indicator');
        const statusText = document.getElementById('status-text');
        indicator.classList.remove('connected');
        statusText.textContent = 'Connection error';
    }
}
