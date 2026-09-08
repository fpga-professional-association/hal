// APB4 requester whose PADDR follows its input while a transfer is in flight.
//
// Identical to apb_requester_ok.v except that the address register's hold mux is
// gone:
//   addr_q' = ADDR_IN                (instead of sel_q ? addr_q : ADDR_IN)
// so PADDR tracks ADDR_IN one cycle later, including during the SETUP-to-ACCESS
// transition and during every wait state. This is the classic "the address
// register was never gated" bug: it is invisible on a zero-wait-state completer
// and corrupts every transfer that is stalled.
//
// Breaks: apb/requester/control_stable_wait and
//         apb/requester/control_stable_setup_to_access.

module apb_requester_broken_stability (
    PCLK,
    PRESETn,
    START,
    ADDR_IN,
    WR_IN,
    WDATA_IN,
    PREADY,
    PSEL,
    PENABLE,
    PADDR_0,
    PWRITE,
    PWDATA_0
);
    input PCLK;
    input PRESETn;
    input START;
    input ADDR_IN;
    input WR_IN;
    input WDATA_IN;
    input PREADY;
    output PSEL;
    output PENABLE;
    output PADDR_0;
    output PWRITE;
    output PWDATA_0;

    wire PCLK;
    wire PRESETn;
    wire START;
    wire ADDR_IN;
    wire WR_IN;
    wire WDATA_IN;
    wire PREADY;
    wire PSEL;
    wire PENABLE;
    wire PADDR_0;
    wire PWRITE;
    wire PWDATA_0;

    wire vdd;
    wire rst;
    wire sel_q;
    wire en_q;
    wire addr_q;
    wire wr_q;
    wire wdat_q;
    wire done;
    wire ndone;
    wire sel_hold;
    wire nsel;
    wire start_sel;
    wire sel_d;
    wire nen;
    wire setup;
    wire npready;
    wire waiting;
    wire en_d;
    wire addr_d;
    wire wr_d;
    wire wdat_d;

    VCC vcc_inst (
        .O(vdd)
    );
    INV rst_inv (
        .I(PRESETn),
        .O(rst)
    );

    BUF psel_buf (
        .I(sel_q),
        .O(PSEL)
    );
    BUF penable_buf (
        .I(en_q),
        .O(PENABLE)
    );
    BUF paddr_buf (
        .I(addr_q),
        .O(PADDR_0)
    );
    BUF pwrite_buf (
        .I(wr_q),
        .O(PWRITE)
    );
    BUF pwdata_buf (
        .I(wdat_q),
        .O(PWDATA_0)
    );

    AND2 done_and (
        .I0(en_q),
        .I1(PREADY),
        .O(done)
    );
    INV done_inv (
        .I(done),
        .O(ndone)
    );
    AND2 sel_hold_and (
        .I0(sel_q),
        .I1(ndone),
        .O(sel_hold)
    );
    INV sel_inv (
        .I(sel_q),
        .O(nsel)
    );
    AND2 start_and (
        .I0(nsel),
        .I1(START),
        .O(start_sel)
    );
    OR2 sel_or (
        .I0(sel_hold),
        .I1(start_sel),
        .O(sel_d)
    );

    INV en_inv (
        .I(en_q),
        .O(nen)
    );
    AND2 setup_and (
        .I0(sel_q),
        .I1(nen),
        .O(setup)
    );
    INV pready_inv (
        .I(PREADY),
        .O(npready)
    );
    AND2 wait_and (
        .I0(en_q),
        .I1(npready),
        .O(waiting)
    );
    OR2 en_or (
        .I0(setup),
        .I1(waiting),
        .O(en_d)
    );

    BUF addr_bypass (
        .I(ADDR_IN),
        .O(addr_d)
    );
    MUX wr_mux (
        .I0(WR_IN),
        .I1(wr_q),
        .S(sel_q),
        .O(wr_d)
    );
    MUX wdat_mux (
        .I0(WDATA_IN),
        .I1(wdat_q),
        .S(sel_q),
        .O(wdat_d)
    );

    FFR sel_reg (
        .C(PCLK),
        .CE(vdd),
        .D(sel_d),
        .R(rst),
        .Q(sel_q)
    );
    FFR en_reg (
        .C(PCLK),
        .CE(vdd),
        .D(en_d),
        .R(rst),
        .Q(en_q)
    );
    FFR addr_reg (
        .C(PCLK),
        .CE(vdd),
        .D(addr_d),
        .R(rst),
        .Q(addr_q)
    );
    FFR wr_reg (
        .C(PCLK),
        .CE(vdd),
        .D(wr_d),
        .R(rst),
        .Q(wr_q)
    );
    FFR wdat_reg (
        .C(PCLK),
        .CE(vdd),
        .D(wdat_d),
        .R(rst),
        .Q(wdat_q)
    );
endmodule
