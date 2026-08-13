package com.githubaiagent.controlplane.assistant;

public class ChatGatewayException extends RuntimeException {

    public ChatGatewayException(String message) {
        super(message);
    }

    public ChatGatewayException(String message, Throwable cause) {
        super(message, cause);
    }
}
