// MilStorage Service - Team 3
// Vulnerable Java Spring Boot application

import org.springframework.web.bind.annotation.*;
import java.io.*;

@RestController
public class MilStorageController {

    // Vulnerability 1: Path traversal in file download
    @GetMapping("/download")
    public String download(@RequestParam String file) {
        try {
            BufferedReader reader = new BufferedReader(new FileReader(file));
            String line;
            StringBuilder content = new StringBuilder();
            while ((line = reader.readLine()) != null) {
                content.append(line);
            }
            reader.close();
            return content.toString();
        } catch (Exception e) {
            return "Error: " + e.getMessage();
        }
    }

    // Vulnerability 2: Hardcoded flag
    private static final String FLAG = "3_3_3_c4d5e6f7g8h9i0j1";

    @GetMapping("/admin")
    public String admin() {
        return "Admin panel: " + FLAG;
    }

    // Vulnerability 3: Deserialization vulnerability
    @PostMapping("/upload")
    public String upload(@RequestBody String data) {
        // Insecure deserialization would go here
        return "Uploaded: " + data.length();
    }
}
