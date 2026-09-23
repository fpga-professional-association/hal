// Testbench for recovered.v.
//   +payload=<file>  a file holding 121 characters '0'/'1' (one attempt)
// Prints one line per rising edge of the read-out window:  <byte> <success>
`timescale 1ns / 1ps

module tb;
    reg clk = 0, rst_n = 0, enable = 0, I = 0;
    wire [7:0] O;
    wire success;

    puzzle dut (.clk(clk), .rst_n(rst_n), .enable(enable), .I(I),
                .O(O), .success(success));

    always #5 clk = ~clk;

    integer fd, ch, k;
    reg [7:0] bits [0:120];
    reg [1023:0] fname;

    initial begin
        if (!$value$plusargs("payload=%s", fname)) begin
            $display("ERROR: need +payload=<file>");
            $finish;
        end
        fd = $fopen(fname, "r");
        if (fd == 0) begin $display("ERROR: cannot open payload"); $finish; end
        k = 0;
        while (k < 121) begin
            ch = $fgetc(fd);
            if (ch == 48 || ch == 49) begin bits[k] = ch - 48; k = k + 1; end
            else if (ch == -1) begin $display("ERROR: short payload"); $finish; end
        end
        $fclose(fd);

        // 3 reset edges, 1 idle edge
        rst_n = 0;
        repeat (3) @(posedge clk);
        @(negedge clk) rst_n = 1;
        @(posedge clk);
        // 121 payload bits
        for (k = 0; k < 121; k = k + 1) begin
            @(negedge clk) begin enable = 1; I = bits[k]; end
            @(posedge clk);
        end
        @(negedge clk) begin enable = 0; I = 0; end
        // read-out
        for (k = 0; k < 20; k = k + 1) begin
            @(posedge clk);
            #1 $display("%0d %0d", O, success);
        end
        $finish;
    end
endmodule
